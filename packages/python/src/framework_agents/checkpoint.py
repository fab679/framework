"""Checkpointed HITL threads + local file locks.

Parity: packages/typescript/src/checkpoint/index.ts — recreated after a
build-script accident wiped the file; EVERY property below is load-bearing
for tests (conversation sessions, gaps, pause/resume examples):

- ThreadId is a single directory name under <root>/threads/: "", ".", ".."
  and anything containing "/" raise THREAD_ID_INVALID (path traversal guard).
- save() OVERWRITES thread_id/saved_at on the wire (camelCase JSON), forcing
  ISO-8601 with ms precision ("2026-10-12T21:50:59.123Z").
- Serialization is STRICT: non-JSON payload values (datetime, Path, models)
  raise CHECKPOINT_SERIALIZABLE — never `default=str`, which would round-trip
  Python reprs into tools on resume.
- with_file_lock: O_CREAT|O_EXCL exclusive create; fresh lock -> "Lock held";
  stale (>stale_ms, default 30000) -> unlink + retry; finally-unlock.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from .errors import AgentError
from .types import ChatMessage, deserialize_messages


def _iso_now() -> str:
    now = datetime.now(timezone.utc)
    return f"{now.strftime('%Y-%m-%dT%H:%M:%S')}.{int(now.microsecond / 1000):03d}Z"


@dataclass
class PendingCall:
    """A suspended tool call awaiting a human decision."""

    interrupt_id: str
    tool: str
    args: Any = None


@dataclass
class ThreadCheckpoint:
    thread_id: str
    saved_at: str
    messages: list[Any] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    pending: list[PendingCall] = field(default_factory=list)


class Checkpointer:
    """Protocol-style base: save/load/delete per threadId (duck-typed)."""

    async def save(self, thread_id: str, checkpoint: ThreadCheckpoint) -> None:  # pragma: no cover
        raise NotImplementedError

    async def load(self, thread_id: str) -> ThreadCheckpoint | None:  # pragma: no cover
        raise NotImplementedError

    async def delete(self, thread_id: str) -> None:  # pragma: no cover
        raise NotImplementedError


def _validate_thread_id(thread_id: str) -> None:
    if thread_id in ("", ".", "..") or "/" in thread_id or "\0" in thread_id:
        raise AgentError("THREAD_ID_INVALID", f"Invalid threadId: {thread_id!r}", retryable=False)


class FileCheckpointer(Checkpointer):
    """JSON-file checkpointer: <root>/threads/<threadId>/checkpoint.json, lock-guarded."""

    def __init__(self, root: str) -> None:
        self.root = Path(root)

    def _file(self, thread_id: str) -> Path:
        _validate_thread_id(thread_id)
        return self.root / "threads" / thread_id / "checkpoint.json"

    def _lock(self, thread_id: str) -> Path:
        _validate_thread_id(thread_id)
        return self.root / "threads" / thread_id / ".lock"

    async def save(self, thread_id: str, checkpoint: ThreadCheckpoint) -> None:
        record = ThreadCheckpoint(
            thread_id=thread_id,
            saved_at=_iso_now(),
            messages=[_dump_message(m) for m in checkpoint.messages],
            state=dict(checkpoint.state or {}),
            pending=[PendingCall(p.interrupt_id, p.tool, p.args) for p in (checkpoint.pending or [])],
        )
        try:
            payload = json.dumps(
                {
                    "threadId": record.thread_id,
                    "savedAt": record.saved_at,
                    "messages": record.messages,
                    "state": record.state,
                    "pending": [
                        {"interruptId": p.interrupt_id, "tool": p.tool, "args": p.args}
                        for p in record.pending
                    ],
                }
            )
        except (TypeError, ValueError) as err:
            # STRICT serialization: a datetime/Path in pending args would
            # otherwise resume as its "__repr__ string" and crash the tool.
            raise AgentError(
                "CHECKPOINT_SERIALIZABLE",
                f"Checkpoint for thread {thread_id!r} contains non-JSON values: {err}",
                retryable=False,
            ) from err

        def write() -> None:
            target = self._file(thread_id)
            target.parent.mkdir(parents=True, exist_ok=True)
            finite_retries = 3
            for _ in range(finite_retries):
                try:
                    with open(target, "w", encoding="utf-8") as fh:
                        fh.write(payload)
                    return
                except OSError:
                    time.sleep(0.01)
            raise AgentError("CHECKPOINT_WRITE", f"Could not write checkpoint for {thread_id!r}", retryable=True)

        async def locked() -> None:
            lock = self._lock(thread_id)

            def body() -> None:
                write()

            await with_file_lock(lock, body)

        await asyncio.to_thread(locked) if False else await locked()

    async def load(self, thread_id: str) -> ThreadCheckpoint | None:
        def read() -> ThreadCheckpoint | None:
            try:
                raw = self._file(thread_id).read_text(encoding="utf-8")
            except (FileNotFoundError, NotADirectoryError):
                return None
            except OSError:
                return None
            try:
                data = json.loads(raw)
            except ValueError:
                return None  # corrupt -> treat as absent (TS matches: JSON.parse try/catch)
            pending = [
                PendingCall(p.get("interruptId", ""), p.get("tool", ""), p.get("args"))
                for p in (data.get("pending") or [])
            ]
            raw_messages = data.get("messages") or []
            messages = [
                m if isinstance(m, ChatMessage) else ChatMessage.model_validate(m)
                for m in raw_messages
            ]
            return ThreadCheckpoint(
                thread_id=data.get("threadId", thread_id),
                saved_at=data.get("savedAt", ""),
                messages=messages,
                state=data.get("state") or {},
                pending=pending,
            )

        return await asyncio.to_thread(read)

    async def delete(self, thread_id: str) -> None:
        def remove() -> None:
            try:
                self._file(thread_id).unlink()
            except (FileNotFoundError, NotADirectoryError, OSError):
                pass  # missing checkpoint is not an error (TS parity)

        async def locked() -> None:
            lock = self._lock(thread_id)
            await with_file_lock(lock, remove)

        await locked()


def _dump_message(message: Any) -> Any:
    dump = getattr(message, "model_dump", None)
    if dump is not None:
        return dump(by_alias=True, exclude_none=True)
    return message


async def with_file_lock(lock_path: Path | str, fn: Callable[[], Awaitable[Any] | Any], stale_ms: float = 30000) -> Any:
    """Exclusive local lock via O_CREAT|O_EXCL. Stale locks (older than
    stale_ms) are reclaimed. Single-machine correctness; distributed locking
    is explicitly out of scope (TS parity, roadmap P3)."""
    path = Path(lock_path)
    remaining = 20  # bounded retry against reclaim races
    while remaining > 0:
        remaining -= 1
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            try:
                age_ms = (time.time() - path.stat().st_mtime) * 1000
            except FileNotFoundError:
                continue  # raced with a releaser; retry acquisition
            if age_ms < stale_ms:
                raise RuntimeError(f"Lock held: {path}")  # plain error: callers may hold for a long time
            try:
                path.unlink()
            except FileNotFoundError:
                continue  # raced with another releaser
        except OSError as err:
            raise AgentError("LOCK_ERROR", f"Lock error on {path}: {err}", retryable=False) from err
    else:
        raise AgentError("LOCK_ERROR", f"Could not acquire lock: {path}", retryable=False)

    try:
        result = fn()
        if asyncio.iscoroutine(result) or asyncio.isfuture(result):
            return await result
        return result
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass  # already released or reclaimed; ignore (TS parity)