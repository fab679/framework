"""Model layer. Parity: src/models/index.ts.

Pinned semantics:
- CompleteOptions.tools carries `parameters_schema` (JSON Schema) — models
  must see real argument shapes; omit only for zero-arg tools.
- ModelCompletion.finish_reason is exposed when the provider reports it
  (truncation observable).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

from ..types import ChatMessage, TokenUsage


@dataclass
class ToolCallRequest:
    id: str
    name: str
    args: Any = None


@dataclass
class ModelCompletion:
    text: str = ""
    thinking: str | None = None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    usage: TokenUsage = field(default_factory=TokenUsage)
    finish_reason: str | None = None


@dataclass
class ToolWire:
    name: str
    description: str
    parameters_schema: dict[str, Any] | None = None


@dataclass
class ResponseFormatWire:
    name: str
    schema: dict[str, Any]
    strict: bool | None = None


@dataclass
class CompleteOptions:
    messages: list[ChatMessage]
    tools: list[ToolWire] = field(default_factory=list)
    response_format: ResponseFormatWire | None = None
    on_token: Callable[[str], None] | None = None
    on_thinking: Callable[[str], None] | None = None
    signal: Any | None = None  # httpx-compatible abort (client-side done in provider)


@runtime_checkable
class ModelProvider(Protocol):
    id: str
    capabilities: dict[str, bool]

    async def complete(self, options: CompleteOptions) -> ModelCompletion: ...


class MockModelProvider:
    """Deterministic mock for tests/examples — no network."""

    id: str = "mock"
    capabilities = {"structured_output": False, "streaming": False}

    def __init__(self) -> None:
        self._calls = 0
        self._seq = itertools.count(1)

    async def complete(self, options: CompleteOptions) -> ModelCompletion:
        self._calls += 1
        text = f"mock-response-{next(self._seq)}"
        options.on_token and options.on_token(text)
        return ModelCompletion(
            text=text,
            tool_calls=[],
            usage=TokenUsage(input_tokens=10, output_tokens=5),
        )


class ScriptedModelProvider:
    """Test stub: returns canned completions in order (last one repeats)."""

    id: str = "scripted"
    capabilities = {"structured_output": False, "streaming": False}

    def __init__(self, completions: list[ModelCompletion]) -> None:
        self._completions = completions
        self._n = 0

    async def complete(self, options: CompleteOptions) -> ModelCompletion:
        self._n += 1
        return self._completions[min(self._n - 1, len(self._completions) - 1)]