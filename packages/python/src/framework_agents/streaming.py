"""Stream collection helpers. Parity: src/streaming/index.ts.

Typed run events with independent projections — consume via
``async for event in agent.stream(input)``.

Python async generators cannot carry return values (unlike the TS generator,
whose final result rides the ``done`` sentinel), so the agent records its
AgentResult in ``_last_result`` BEFORE yielding done and ``AgentStream`` (what
``agent.stream()`` returns) captures it PER STREAM at the moment done is
observed, exposing it via ``.result``. ``collect_stream`` buckets both.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, TypedDict

from .errors import AgentError


class StreamEvent(TypedDict, total=False):
    type: str  # "step" | "token" | "thinking" | "tool_call" | "tool_result" | "done"
    step: int
    delta: str
    name: str
    args: Any
    output: Any
    text: str


class AgentStream:
    """Async-iterator over run events; the finished AgentResult is reachable
    via ``.result`` (None until a run completes).

    The result is captured PER STREAM at the moment the done event is
    observed (synchronously — no await between observation and capture), so
    concurrent streams on ONE agent each keep their own result and a
    done-break never returns another run's stale result."""

    def __init__(self, agent: Any, gen: AsyncIterator[StreamEvent]) -> None:
        self._agent = agent
        self._gen = gen
        self._result: Any = None

    def __aiter__(self) -> "AgentStream":
        return self

    async def __anext__(self) -> StreamEvent:
        event = await self._gen.__anext__()
        if event.get("type") == "done":
            # Per-stream capture: the agent records _last_result BEFORE the
            # done event is yielded, so this read lands on THIS run's result.
            self._result = getattr(self._agent, "_last_result", None)
        return event

    async def aclose(self) -> None:
        await self._gen.aclose()

    async def athrow(self, typ: Any, val: Any = None, tb: Any = None) -> Any:
        return await self._gen.athrow(typ, val, tb)

    @property
    def result(self) -> Any:
        return self._result


async def collect_stream(stream: AsyncIterator[StreamEvent]) -> dict[str, Any]:
    """Collect a stream into buckets — handy for tests and simple UIs."""
    events: list[StreamEvent] = []
    async for event in stream:
        events.append(event)
    result = getattr(stream, "result", None)
    if result is None:
        raise AgentError("RUN_INCOMPLETE", "Stream ended without a result", retryable=False)
    return {"events": events, "result": result}