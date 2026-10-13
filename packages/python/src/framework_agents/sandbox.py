"""Sandboxed code execution tools. Parity: src/sandbox/index.ts.

- run_javascript: exec in a restricted namespace (limited builtins — no
  import/open/eval/process access), timeout enforced via asyncio.wait_for
  cancel semantics, output = last expression value + printed output,
  truncated.
- create_sandbox_backend: one rooted workspace with a shell `execute` tool
  (cwd=root, scrubbed env — no secrets/proxies inherited, timeout, output
  caps enforced INCREMENTALLY — the child is killed the moment accumulated
  output crosses max_buffer, TS kills at 4MB — with a spill note).

Isolation, honestly scoped: this is a speed bump, NOT a (container) security
boundary. Never put secrets in the sandbox env, gate `execute` with
human-in-the-loop for sensitive work, treat outputs as untrusted.
"""

from __future__ import annotations

import asyncio
import ast
import io
import json
import math
import os
import threading
from typing import Any, Optional

from pydantic import BaseModel, Field

from .errors import AgentError
from .tools import Tool, ToolContext, define_tool

SCRUBBED_ENV_KEYS = ("PATH", "HOME", "LANG", "LC_ALL", "TZ", "TERM", "NO_COLOR")

# Safe subset only — no open/exec/eval/__import__/getattr/globals.
_SAFE_BUILTINS: dict[str, Any] = {
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "bytes": bytes,
    "chr": chr,
    "dict": dict,
    "divmod": divmod,
    "enumerate": enumerate,
    "filter": filter,
    "float": float,
    "frozenset": frozenset,
    "hash": hash,
    "hex": hex,
    "int": int,
    "isinstance": isinstance,
    "len": len,
    "list": list,
    "map": map,
    "max": max,
    "min": min,
    "oct": oct,
    "ord": ord,
    "pow": pow,
    "range": range,
    "repr": repr,
    "reversed": reversed,
    "round": round,
    "set": set,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
    "zip": zip,
}


class _JavascriptArgs(BaseModel):
    code: str = Field(min_length=1, max_length=8000)


class _ExecuteArgs(BaseModel):
    command: str = Field(min_length=1, max_length=4000)


def _sleep(seconds: float) -> None:
    """Blocking sleep for sandboxed code (blocks only the sandbox thread)."""
    threading.Event().wait(max(0.0, float(seconds)))


def _exec_in_sandbox(code: str) -> tuple[str, Any]:
    """Run code in the restricted namespace -> (printed output, result value)."""
    buffer = io.StringIO()

    def sandbox_print(*args: Any, sep: str = " ", end: str = "\n", file: Any = None, flush: bool = False) -> None:
        print(*args, sep=sep, end=end, file=buffer, flush=flush)

    namespace: dict[str, Any] = {
        "__builtins__": _SAFE_BUILTINS,
        "print": sandbox_print,
        "sleep": _sleep,
        "math": math,
        "json": json,
    }
    tree = ast.parse(code, mode="exec")
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        # Completion-value semantics (JS parity): the last expression
        # statement becomes the result.
        if len(tree.body) > 1:
            exec(compile(ast.Module(body=tree.body[:-1], type_ignores=[]), "<sandbox>", "exec"), namespace)  # noqa: S102
        result = eval(compile(ast.Expression(tree.body[-1].value), "<sandbox>", "eval"), namespace)  # noqa: S307
    else:
        exec(compile(tree, "<sandbox>", "exec"), namespace)  # noqa: S102
        result = None
    return buffer.getvalue(), result


async def _run_javascript(code: str, timeout_ms: float, max_output_chars: int) -> str:
    """Exec in a daemon worker thread; asyncio.wait_for enforces the timeout.

    Python cannot interrupt exec mid-statement, so a timeout ABANDONS the
    worker (daemon — it cannot keep the process alive) and raises
    SANDBOX_TIMEOUT, mirroring the TS vm timeout contract.
    """

    def unpacked() -> str:
        printed, result = _exec_in_sandbox(code)
        pieces: list[str] = []
        if printed.rstrip("\n"):
            pieces.append(printed.rstrip("\n"))
        if result is not None:
            rendered = result if isinstance(result, str) else json.dumps(result, default=str)
            pieces.append(rendered)
        out = "\n".join(pieces)
        if len(out) > max_output_chars:
            out = out[:max_output_chars] + "…[truncated]"
        return out

    box: dict[str, Any] = {}
    done = threading.Event()

    def work() -> None:
        try:
            box["value"] = unpacked()
        except BaseException as err:  # noqa: BLE001 - surfaced on the caller side
            box["error"] = err
        finally:
            done.set()

    worker = threading.Thread(target=work, daemon=True, name="sandbox-javascript")
    worker.start()

    async def await_done() -> None:
        while not done.is_set():
            await asyncio.sleep(0.005)

    try:
        await asyncio.wait_for(await_done(), max(timeout_ms, 1) / 1000)
    except (asyncio.TimeoutError, TimeoutError):
        raise AgentError("SANDBOX_TIMEOUT", f"Sandbox exceeded {timeout_ms}ms", retryable=True)
    if "error" in box:
        raise AgentError("SANDBOX_ERROR", f"Sandbox failed: {box['error']}", retryable=False) from box["error"]
    return box["value"]


def run_javascript_tool(timeout_ms: float = 2000, max_output_chars: int = 4000) -> Tool:
    """`run_javascript` Tool: sandboxed exec, no network/fs, timeout, caps."""

    async def execute(args: Any, ctx: Optional[ToolContext]) -> str:
        code = args.code if isinstance(args, _JavascriptArgs) else args["code"]
        return await _run_javascript(code, timeout_ms, max_output_chars)

    return define_tool(
        name="run_javascript",
        description=(
            "Execute JavaScript-like code in a sandbox (no network, no fs, timeout). "
            "The last expression's value becomes the result."
        ),
        schema=_JavascriptArgs,
        execute=execute,
    )


def _scrub_env(extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    env: dict[str, str] = {}
    for key in SCRUBBED_ENV_KEYS:
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    env.update(extra or {})
    return env


class _OutputOverflow(Exception):
    """Internal: accumulated child output crossed ``max_buffer`` mid-run."""


class SandboxBackend:
    """Local-first sandbox workspace: shell `execute` rooted at `root`."""

    def __init__(
        self,
        root: str,
        timeout_ms: float = 30000,
        max_buffer: int = 4 * 1024 * 1024,
        max_output_chars: int = 12000,
        env: Optional[dict[str, str]] = None,
    ) -> None:
        self.root = os.path.abspath(root)
        self.timeout_ms = timeout_ms
        self.max_buffer = max_buffer
        self.max_output_chars = max_output_chars
        self._env = _scrub_env(env)
        self._spills = 0

    def tools(self) -> list[Tool]:
        return [self._execute_tool()]

    def _spill(self, text: str) -> str:
        self._spills += 1
        name = f".spill-{self._spills}.txt"
        with open(os.path.join(self.root, name), "w", encoding="utf-8") as handle:
            handle.write(text)
        return f"{text[: self.max_output_chars]}\n…[truncated {len(text) - self.max_output_chars} chars → read {name}]"

    @staticmethod
    def _kill(proc: asyncio.subprocess.Process) -> None:
        try:
            proc.kill()
        except ProcessLookupError:  # pragma: no cover - already dead
            pass

    async def _drain_and_wait(self, proc: asyncio.subprocess.Process) -> tuple[bytes, bytes]:
        """Read the child's output INCREMENTALLY (both pipes concurrently),
        aborting at ``max_buffer`` MID-STREAM — unlike communicate(), which
        buffers everything (a `yes` loop would fill RAM until the timeout).
        The overflow abort cancels the sibling pump through the TaskGroup."""
        total = 0
        out: list[bytes] = []
        err: list[bytes] = []

        async def pump(bucket: list[bytes], stream: Optional[asyncio.StreamReader]) -> None:
            nonlocal total
            assert stream is not None  # both pipes are PIPE in _run_command
            while True:
                chunk = await stream.read(64 * 1024)
                if not chunk:
                    return
                bucket.append(chunk)
                total += len(chunk)
                if total > self.max_buffer:
                    raise _OutputOverflow

        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(pump(out, proc.stdout))
                tg.create_task(pump(err, proc.stderr))
                await proc.wait()
        except ExceptionGroup as eg:
            # Both pipes can cross the cap in one event-loop pass; unwrap an
            # overflow-only group so _run_command sees the bare signal.
            overflows = [e for e in eg.exceptions if isinstance(e, _OutputOverflow)]
            if len(overflows) == len(eg.exceptions):
                raise (overflows[0] if len(overflows) == 1 else _OutputOverflow()) from eg
            raise
        return b"".join(out), b"".join(err)

    async def _run_command(self, command: str) -> str:
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=self.root,
                env=self._env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as err:  # unusable cwd / shell
            raise AgentError("SANDBOX_ERROR", f"Cannot run command: {err}", retryable=False) from err
        try:
            stdout, stderr = await asyncio.wait_for(self._drain_and_wait(proc), self.timeout_ms / 1000)
        except _OutputOverflow:
            # The cap fires DURING the run: kill the child immediately (TS
            # kills at 4MB) instead of buffering until the timeout.
            self._kill(proc)
            await proc.wait()
            raise AgentError(
                "SANDBOX_OUTPUT_TOO_LARGE",
                "Command output exceeded the buffer; write output to a file and read it in chunks",
                retryable=False,
            )
        except (asyncio.TimeoutError, TimeoutError):
            self._kill(proc)
            await proc.wait()
            raise AgentError("SANDBOX_TIMEOUT", f"Command exceeded {self.timeout_ms}ms", retryable=True)
        code = proc.returncode or 0
        text = stdout.decode("utf-8", errors="replace")
        if stderr:
            text += f"\n[stderr]\n{stderr.decode('utf-8', errors='replace')}"
        if len(text) > self.max_output_chars:
            text = self._spill(text)
        if code != 0:
            text += f"\n[Command failed with exit code {code}]"
        return text or "[Command succeeded with exit code 0]"

    def _execute_tool(self) -> Tool:
        backend = self

        async def execute(args: Any, ctx: Optional[ToolContext]) -> str:
            command = args.command if isinstance(args, _ExecuteArgs) else args["command"]
            return await backend._run_command(command)

        return define_tool(
            name="execute",
            description=(
                "Run a shell command in the sandbox workspace (cwd=root, scrubbed env, "
                "timeout). Large output spills to a file."
            ),
            schema=_ExecuteArgs,
            execute=execute,
        )


def create_sandbox_backend(
    root: str,
    timeout_ms: float = 30000,
    max_buffer: int = 4 * 1024 * 1024,
    max_output_chars: int = 12000,
    env: Optional[dict[str, str]] = None,
) -> SandboxBackend:
    """Sandbox backend (execute tool) rooted at `root` (shell cwd jail)."""
    return SandboxBackend(root=root, timeout_ms=timeout_ms, max_buffer=max_buffer, max_output_chars=max_output_chars, env=env)