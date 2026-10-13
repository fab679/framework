"""Console observability hooks. Parity: src/observability/index.ts.

AgentEvents-shaped console logging + a red error line. Colors only when
stdout is a TTY and NO_COLOR is unset (paint-inert otherwise). Hooks are
plain (async-safe) print calls: the agent loop awaits nothing here, so
they may be sync or async-friendly callables with no blocking work.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Optional

from .errors import AgentError

_ANSI = {
    "reset": "\x1b[0m",
    "bold": "\x1b[1m",
    "dim": "\x1b[2m",
    "cyan": "\x1b[36m",
    "yellow": "\x1b[33m",
    "green": "\x1b[32m",
    "red": "\x1b[31m",
}


def supports_color() -> bool:
    """No NO_COLOR, and stdout is a TTY."""
    if os.environ.get("NO_COLOR"):
        return False
    try:
        return bool(sys.stdout.isatty())
    except (AttributeError, ValueError):  # pragma: no cover - exotic stdouts
        return False


def _paint(color: str, text: str, on: bool) -> str:
    if not on or color == "reset":
        return text
    return f"{_ANSI[color]}{text}{_ANSI['reset']}"


class ConsoleEvents:
    """AgentEvents-shaped console logger (step/token/tool/done lines)."""

    def __init__(self, prefix: str = "[agent]", colors: Optional[bool] = None) -> None:
        self.prefix = prefix
        self.colors = supports_color() if colors is None else bool(colors)

    def _tag(self, color: str, label: str) -> str:
        return _paint(color, f"{self.prefix} {label}", self.colors)

    def on_step(self, step: int, messages: Any = None) -> None:
        detail = f" ({len(messages)} msgs)" if messages is not None else ""
        print(self._tag("cyan", f"step {step}{detail}"))

    def on_tool_call(self, name: str, args: Any) -> None:
        print(self._tag("yellow", f"tool:call {name}"), args)

    def on_tool_result(self, name: str, output: Any) -> None:
        print(self._tag("green", f"tool:result {name}"), output)

    def on_done(self, text: str, tool_calls: Any = None) -> None:
        print(self._tag("green", _paint("bold", "done:", self.colors)), text)

    def on_token(self, delta: str) -> None:
        print(delta, end="", flush=True)

    def on_thinking(self, delta: str) -> None:
        print(_paint("dim", delta, self.colors), end="", flush=True)


def console_events(prefix: str = "[agent]", colors: Optional[bool] = None) -> ConsoleEvents:
    """AgentEvents-shaped console logger; `colors=None` => TTY without NO_COLOR."""
    return ConsoleEvents(prefix=prefix, colors=colors)


def log_error(prefix: str, err: BaseException, colors: Optional[bool] = None) -> None:
    """Red error line (pairs with console_events): code + message."""
    on = supports_color() if colors is None else bool(colors)
    code = err.code if isinstance(err, AgentError) else "ERROR"
    message = str(err) or err.__class__.__name__
    print(f"{_paint('red', f'{prefix} error:', on)} [{code}] {message}", file=sys.stderr)