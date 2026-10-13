"""framework.json — declare agents, launch them as A2A servers.

Parity: packages/typescript/src/serve/index.ts + src/serve/cli.ts.

Relative paths resolve against the config file's directory; agent entries
that reference another file resolve pathy keys against THAT file's dir,
with inline overrides resolving against framework.json — key by key, the
same merge the TS schema performs.

Deviations from TS serve semantics (deliberate, port-level):

- Agents are built LAZILY: ``init_chat_model`` runs inside the A2A
  contextId-scoped agent factory, never at boot (TS builds one agent per
  def while serving; the Python port constructs only the card at boot so
  a missing API key cannot take the whole port ladder down).
- Env files (framework-level AND per-agent) load with setdefault
  semantics — a real environment variable is NEVER clobbered (the same
  pattern config.py uses). In TS, agent-level files load with dotenv
  ``override: true`` and can beat real env; here the process env always
  wins and, between two files, the FIRST loaded wins.
- ``SessionManager`` support in AgentOptions has not been ported into
  the agent loop yet; the per-context manager is attached to the built
  Agent instance as a ``session_manager`` attribute (sessionId
  ``"{name}:{contextId}"``) so the wiring is reserved and ready.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .a2a_server import A2AServer
from .agent import Agent, AgentOptions
from .checkpoint import FileCheckpointer
from .config import init_chat_model
from .sessions import LocalFileStorage, SessionManager
from .skills import SkillRegistry, discover_skills

DEFAULT_SYSTEM_PROMPT = "You are a helpful agent. Use tools when needed. Be concise."

_CAMEL = ConfigDict(populate_by_name=True)

# ---------------------------------------------------------------------------
# framework.json schema (pydantic mirror of the zod schemas)
# ---------------------------------------------------------------------------


class AgentDef(BaseModel):
    model_config = _CAMEL

    name: str = Field(min_length=1)
    description: str = ""
    system_prompt: str = Field(default=DEFAULT_SYSTEM_PROMPT, validation_alias="systemPrompt")
    model: str
    port: int | None = Field(default=None, gt=0)
    skills: list[str] = Field(default_factory=list)
    filesystem: str | None = None
    todos: bool = False
    max_steps: int = Field(default=8, validation_alias="maxSteps", gt=0)
    session_dir: str | None = Field(default=None, validation_alias="sessionDir")
    """Extra env file for this agent (never clobbers real environment)."""
    env_file: str | None = Field(default=None, validation_alias="envFile")


class ServerConfig(BaseModel):
    model_config = _CAMEL

    host: str = "127.0.0.1"
    base_port: int = Field(default=8484, validation_alias="basePort", ge=0)


class HttpConfig(BaseModel):
    model_config = _CAMEL

    cors_origins: list[str] = Field(default_factory=list, validation_alias="corsOrigins")


class CheckpointerConfig(BaseModel):
    dir: str


class FrameworkFile(BaseModel):
    model_config = _CAMEL

    server: ServerConfig = Field(default_factory=ServerConfig)
    """Framework-level env file(s), loaded before agents resolve models."""
    env_file: Union[str, list[str]] | None = Field(default=None, validation_alias="envFile")
    """Inline env mapping (applied after env files, overrides them)."""
    env: dict[str, str] | None = None
    """Local checkpoint dir for served agents (durable HITL threads)."""
    checkpointer: CheckpointerConfig | None = None
    http: HttpConfig = Field(default_factory=HttpConfig)
    # Entries are inline agent objects or {"file": ...} refs (+ optional
    # inline overrides); parsed entry-by-entry in load_framework_config.
    agents: list[dict[str, Any]] = Field(min_length=1)


# ---------------------------------------------------------------------------
# Loaded config (every entry resolved to a concrete AgentDef)
# ---------------------------------------------------------------------------


@dataclass
class LoadedFrameworkFile:
    server: ServerConfig
    env_file: Union[str, list[str]] | None
    env: dict[str, str] | None
    checkpointer: CheckpointerConfig | None
    http: HttpConfig
    agents: list[AgentDef]


def load_env_file(path: str) -> None:
    """Load an env file if present (silent when missing). Same pattern as
    config.py: setdefault, so real environment variables always win."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _abs(base: Path, value: str) -> str:
    return value if os.path.isabs(value) else str((base / value).resolve())


def _resolve_agent(defn: AgentDef, base_for: Callable[[str], Path]) -> AgentDef:
    """Resolve relative paths per pathy key, against the dir `base_for`
    picks for that key (camelCase key name, as in the raw JSON)."""
    update: dict[str, Any] = {}
    if defn.skills:
        update["skills"] = [_abs(base_for("skills"), s) for s in defn.skills]
    if defn.filesystem:
        update["filesystem"] = _abs(base_for("filesystem"), defn.filesystem)
    if defn.session_dir:
        update["session_dir"] = _abs(base_for("sessionDir"), defn.session_dir)
    if defn.env_file:
        update["env_file"] = _abs(base_for("envFile"), defn.env_file)
    return defn.model_copy(update=update) if update else defn


def load_framework_config(path: str) -> LoadedFrameworkFile:
    """Parse framework.json, resolve relative paths against its directory,
    and load env files (setdefault — the real environment wins)."""
    config_path = Path(path).resolve()
    parsed = FrameworkFile.model_validate(json.loads(config_path.read_text(encoding="utf-8")))
    dir = config_path.parent

    env_files = parsed.env_file if isinstance(parsed.env_file, list) else [parsed.env_file] if parsed.env_file else []
    for env_file in env_files:
        # Framework-level files fill gaps only — real environment variables win.
        load_env_file(_abs(dir, env_file))
    for key, value in (parsed.env or {}).items():
        os.environ[key] = value

    agents: list[AgentDef] = []
    for entry in parsed.agents:
        if isinstance(entry, dict) and isinstance(entry.get("file"), str) and "model" not in entry:
            # `{ "file": "./agents/helper.json" }` (+ optional inline overrides).
            agent_path = Path(_abs(dir, entry["file"]))
            file_raw = json.loads(agent_path.read_text(encoding="utf-8"))
            if not isinstance(file_raw, dict):
                raise ValueError(f"agent file must contain an object: {entry['file']}")
            overrides = {k: v for k, v in entry.items() if k != "file"}
            merged = AgentDef.model_validate({**file_raw, **overrides})
            file_keys = set(file_raw)

            def base_for(camel_key: str, file_keys=file_keys, overrides=overrides, file_dir=agent_path.parent) -> Path:
                # Merge file-owned keys (resolved against the agent file's dir)
                # with inline overrides (framework dir).
                if camel_key in file_keys and camel_key not in overrides:
                    return file_dir
                return dir

            resolved = _resolve_agent(merged, base_for)
        else:
            # Inline def (a `file` key alongside a `model` is dropped, exactly
            # like the TS union, where AgentDefSchema matches first).
            resolved = _resolve_agent(AgentDef.model_validate(entry), lambda _key: dir)
        if resolved.env_file:
            load_env_file(resolved.env_file)
        agents.append(resolved)

    return LoadedFrameworkFile(
        server=parsed.server,
        env_file=parsed.env_file,
        env=parsed.env,
        checkpointer=CheckpointerConfig(dir=_abs(dir, parsed.checkpointer.dir)) if parsed.checkpointer else None,
        http=parsed.http,
        agents=agents,
    )


# ---------------------------------------------------------------------------
# Agent construction
# ---------------------------------------------------------------------------


def _filesystem_middleware(root: str) -> Any | None:
    """filesystem middleware is added in parallel; use it only when the
    attribute exists (guarded, never a hard dependency)."""
    from . import middleware as _middleware

    factory = getattr(_middleware, "filesystem_middleware", None)
    if factory is None:
        return None
    try:
        return factory(root=root)
    except TypeError:
        return factory(root)


def _middlewares_for(defn: AgentDef) -> list[Any]:
    middlewares: list[Any] = []
    fs = _filesystem_middleware(defn.filesystem) if defn.filesystem else None
    if fs is not None:
        middlewares.append(fs)
    if defn.todos:
        from . import middleware as _middleware

        todo_list = getattr(_middleware, "todo_list", None)
        if todo_list is not None:
            middlewares.append(todo_list())
    return middlewares


@dataclass
class BuiltAgent:
    agent: Agent
    card: dict[str, Any]


async def build_served_agent(defn: AgentDef, session_id: str | None = None) -> BuiltAgent:
    """Skill discovery -> catalog appended to the system prompt; middleware
    assembly (filesystem + todos when available); session manager on
    sessionDir with the given per-context session id."""
    system_prompt = defn.system_prompt
    if defn.skills:
        registry = SkillRegistry()
        for skill_dir in defn.skills:
            for skill in discover_skills([skill_dir]):
                try:
                    registry.register(skill)
                except ValueError:
                    pass  # duplicate skill names across dirs: first wins
        catalog = "\n".join(f"- {s.name}: {s.description}" for s in registry.list())
        if catalog:
            system_prompt += f"\n\nAvailable skill packs (mention $name to invoke):\n{catalog}"

    agent = Agent(
        AgentOptions(
            model=init_chat_model(defn.model),
            name=defn.name,
            description=defn.description,
            system_prompt=system_prompt,
            max_steps=defn.max_steps,
            middleware=_middlewares_for(defn),
        )
    )
    if defn.session_dir:
        # Context-scoped id keeps one def's sessions from mingling when the
        # server runs a fresh agent per A2A contextId.
        agent.session_manager = SessionManager(  # type: ignore[attr-defined]
            storage=LocalFileStorage(defn.session_dir),
            session_id=session_id or defn.name,
        )
    card: dict[str, Any] = {"name": defn.name}
    if defn.description:
        card["description"] = defn.description
    return BuiltAgent(agent=agent, card=card)


# ---------------------------------------------------------------------------
# Serving
# ---------------------------------------------------------------------------


@dataclass
class ServedAgent:
    name: str
    url: str
    card_url: str
    server: A2AServer


@dataclass
class FrameworkApp:
    """Running fleet from serve_framework(): {agents: [{name, url, card_url,
    server}], stop()}."""

    agents: list[ServedAgent]

    async def stop(self) -> None:
        for entry in self.agents:
            await entry.server.stop()


async def serve_framework(file: Union[LoadedFrameworkFile, str, Path]) -> FrameworkApp:
    """Boot every declared agent as an A2AServer. Ports assign sequentially
    from base_port (an explicit agent port wins; the next agent continues at
    the previous actual port + 1)."""
    loaded = load_framework_config(str(file)) if isinstance(file, (str, Path)) else file
    checkpointer = FileCheckpointer(loaded.checkpointer.dir) if loaded.checkpointer else None
    served: list[ServedAgent] = []
    port = loaded.server.base_port
    for defn in loaded.agents:
        card: dict[str, Any] = {"name": defn.name}
        if defn.description:
            card["description"] = defn.description

        def agent_factory(context_id: str, defn: AgentDef = defn) -> Any:
            # Fresh agent per A2A contextId — sharing one instance would leak
            # one conversation into another context's history.
            async def build() -> Agent:
                built = await build_served_agent(
                    defn,
                    session_id=f"{defn.name}:{context_id}" if defn.session_dir else None,
                )
                return built.agent

            return build()

        server = A2AServer(
            card,
            agent_factory,
            checkpointer=checkpointer,
            cors_origins=loaded.http.cors_origins,
        )
        actual = await server.start(host=loaded.server.host, port=defn.port if defn.port is not None else port)
        port = actual + 1
        url = f"http://{loaded.server.host}:{actual}"
        served.append(ServedAgent(name=defn.name, url=url, card_url=f"{url}/.well-known/agent-card.json", server=server))
    return FrameworkApp(agents=served)


# ---------------------------------------------------------------------------
# CLI (dev / serve / validate — mirrors the TS cli)
# ---------------------------------------------------------------------------

HELP = """framework — launch Framework agents as local servers.

Usage:
  python -m framework_agents.serve [command] [options]

Commands:
  dev [-c framework.json] [--host H] [--port N]   local dev server (default)
  serve|up [-c framework.json]                     serve agents
  validate [-c framework.json]                     validate config and print agents
  --help                                           this help

framework.json keys: server{host,basePort}, envFile, env{}, checkpointer{dir},
http{corsOrigins}, agents[{name, model, file?, envFile?, skills?, filesystem?,
todos?, maxSteps?, sessionDir?, port?}]. Agent files + relative paths resolve
against their own file's directory; inline overrides against framework.json.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="framework-serve", description=HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", nargs="?", default="dev", choices=["dev", "serve", "up", "validate"])
    parser.add_argument("-c", "--config", default="framework.json", help="path to framework.json")
    parser.add_argument("--host", default=None, help="bind host (overrides server.host)")
    parser.add_argument("--port", type=int, default=None, help="base port (overrides server.basePort)")
    args = parser.parse_args(argv)

    config_path = os.path.abspath(args.config)
    try:
        file = load_framework_config(config_path)
    except (OSError, ValueError, ValidationError) as err:
        print(f"[framework] config error: {err}", file=sys.stderr)
        return 1

    if args.command == "validate":
        print(f"OK: {config_path}")
        for a in file.agents:
            suffix = f" :{a.port}" if a.port else ""
            env_note = f" (env: {a.env_file})" if a.env_file else ""
            print(f"- {a.name} [{a.model}]{suffix}{env_note}")
        return 0

    # Validation INSIDE the try: an invalid --port must be a friendly line on
    # stderr + exit 1 (TS cli parity), never a raw pydantic traceback.
    try:
        server = ServerConfig(host=args.host or file.server.host, base_port=args.port if args.port is not None else file.server.base_port)
    except ValidationError as err:
        print(f"[framework] Invalid --port: {err}", file=sys.stderr)
        return 1
    return asyncio.run(_run(file, server, args.command))


async def _run(file: LoadedFrameworkFile, server: ServerConfig, command: str) -> int:
    loaded = file
    loaded.server = server
    app = await serve_framework(loaded)
    for a in app.agents:
        print(f"[framework] {a.name} -> {a.url} (card: {a.card_url})")
    print(f"[framework] {'dev server' if command == 'dev' else 'serving'} {len(app.agents)} agent(s). Ctrl-C to stop.")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed = False
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)
        installed = True
    except (NotImplementedError, RuntimeError):  # pragma: no cover - non-unix
        pass
    try:
        await stop_event.wait()  # sits here until Ctrl-C / SIGTERM
    except KeyboardInterrupt:  # pragma: no cover - non-unix fallback
        pass
    finally:
        if installed:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)
    await app.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))