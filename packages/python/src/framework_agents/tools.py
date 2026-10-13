"""Tools: pydantic-validated args. Parity: src/tools/index.ts."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Type

from pydantic import BaseModel, ValidationError

from .errors import AgentError
from .state import AgentState


@dataclass
class ToolContext:
    """Arg #2 for tools that need state (agent name / agent state / invocation
    state). The agent loop wires its AgentState and the LIVE per-run
    invocation_state (by reference) at each dispatch, like the TS ``toolCtx``."""

    agent_name: str = "agent"
    # The agent's AgentState (dict-like via .get/.set) — defaults to a fresh
    # AgentState for standalone/registry calls.
    state: AgentState = field(default_factory=AgentState)
    invocation_state: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResult:
    """Split raw output: model-visible content vs downstream-only artifact."""
    content: Any
    artifact: Any = None


async def _default_to_result(output: Any) -> ToolResult:
    return ToolResult(content=output if isinstance(output, str) else _jsonish(output))


def _jsonish(value: Any) -> str:
    import json

    return json.dumps(value, default=str)


@dataclass
class Tool:
    name: str
    description: str
    schema: Type[BaseModel]  # pydantic model = the validated arg shape
    execute: Callable[[Any, ToolContext | None], Awaitable[Any]]
    to_result: Callable[[Any], Awaitable[ToolResult]] = _default_to_result


def define_tool(
    *,
    name: str,
    description: str,
    schema: Type[BaseModel],
    execute: Callable[[Any, ToolContext | None], Awaitable[Any]],
    to_result: Optional[Callable[[Any], Awaitable[ToolResult]]] = None,
) -> Tool:
    if not name:
        raise ValueError("Tool must have a name")
    return Tool(
        name=name,
        description=description,
        schema=schema,
        execute=execute,
        to_result=to_result or _default_to_result,
    )


def parameters_schema(tool: Tool) -> dict[str, Any] | None:
    """JSON Schema for the wire — tools without it are advertised zero-arg.

    Parity with the TS fix: the model must see real argument shapes.
    """
    try:
        schema = tool.schema.model_json_schema()
        return schema
    except Exception:  # pragma: no cover - exotic pydantic setups
        return None


class ToolRegistry:
    """Validating registry — single place tools are looked up + arg-checked."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> "ToolRegistry":
        if tool.name in self._tools:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool
        return self

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def list(self) -> list[Tool]:
        return list(self._tools.values())

    async def call(self, name: str, raw_args: Any, ctx: ToolContext | None = None) -> Any:
        """Validate args and execute. ``ctx`` is optional (None stays None —
        callers may pass a ToolContext so registry calls don't force None on
        tools that read ctx)."""
        tool = self._tools.get(name)
        if tool is None:
            raise AgentError("UNKNOWN_TOOL", f"Unknown tool: {name}", retryable=False)
        try:
            args = tool.schema.model_validate(raw_args if isinstance(raw_args, dict) else {})
        except ValidationError as err:
            raise AgentError("TOOL_ARG_INVALID", f'Invalid args for tool "{name}": {err}', retryable=False) from err
        return await tool.execute(tool.schema.model_validate(args.model_dump()), ctx)