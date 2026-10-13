"""OTel-shaped tracing exporter. Parity: src/tracing/index.ts (span half).

Spans from AgentEvents, sunk to console/JSONL/memory. No OTel SDK — the
Span shape is transport-compatible, the sinks are local-first.

Pinned (TS post-fix):
- Spans are emitted DURATION-COMPLETE (started_at + ended_at both set at
  emit time): no dangling open-span bookkeeping.
- Span ids are unique hex (secrets) — sequential per sink, no name-keyed map.
- run.done metrics + tool.result parent linkage. CAVEAT (parity with the TS
  caveat): the parent is the LAST tool.call span emitted on THIS instance —
  there is no run-root, so concurrent interleaved runs through one sink can
  cross-link a tool.result to another run's tool.call. Fixing that (a run-root
  span) is pending in the TS track too; keep it simple here.
- Coverage: ALL events (step/token/thinking/tool calls) — the TS bug lost
  everything but tool calls / partial data; emit spans for every hook.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Protocol


@dataclass
class Span:
    trace_id: str
    span_id: str
    name: str
    started_at: str  # ISO timestamp
    ended_at: Optional[str] = None  # ISO timestamp; duration-complete spans set both
    parent_id: Optional[str] = None
    attributes: dict[str, Any] = field(default_factory=dict)


class SpanSink(Protocol):
    def emit(self, span: Span) -> None: ...


class ConsoleSpanSink:
    def emit(self, span: Span) -> None:
        print(f"[trace] {span.name}", json.dumps(span.attributes))


class JsonlSpanSink:
    """Per-span JSON lines, appending; the parent dir is created (mkdir -p)."""

    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)

    def emit(self, span: Span) -> None:
        record = {
            "traceId": span.trace_id,
            "spanId": span.span_id,
            "parentId": span.parent_id,
            "name": span.name,
            "startedAt": span.started_at,
            "endedAt": span.ended_at,
            "attributes": span.attributes,
        }
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")


class MemorySpanSink:
    def __init__(self) -> None:
        self.spans: list[Span] = []

    def emit(self, span: Span) -> None:
        self.spans.append(span)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TracingEvents:
    """AgentEvents-shaped projections of run/step/tool lifecycle into spans."""

    def __init__(self, sink: SpanSink, trace_id: Optional[str] = None) -> None:
        self.sink = sink
        self.trace_id = trace_id or secrets.token_hex(16)
        # Sequential unique span ids only — no name-keyed map on this instance
        # beyond the LAST tool.call (name-keyed maps leak across runs).
        self._last_tool_call_span_id: Optional[str] = None

    def _emit(self, name: str, attributes: dict[str, Any], parent_id: Optional[str] = None) -> Span:
        now = _now_iso()
        span = Span(
            trace_id=self.trace_id,
            span_id=secrets.token_hex(8),  # 16 hex chars, unique per emission
            parent_id=parent_id,
            name=name,
            started_at=now,
            ended_at=now,  # duration-complete at emit time
            attributes=attributes,
        )
        self.sink.emit(span)
        return span

    def on_step(self, step: int) -> None:
        self._emit("step.start", {"step": step})

    def on_token(self, delta: str) -> None:
        self._emit("model.token", {"delta": delta[:200]})

    def on_thinking(self, delta: str) -> None:
        self._emit("model.thinking", {"delta": delta[:200]})

    def on_tool_call(self, name: str, args: Any) -> None:
        span = self._emit("tool.call", {"tool": name, "args": args})
        self._last_tool_call_span_id = span.span_id

    def on_tool_result(self, name: str, output: Any) -> None:
        # Parent = the most recent tool.call span on this instance (see the
        # module caveat: no run root, so concurrent runs through one sink can
        # cross-link — pending in the TS track as well).
        self._emit("tool.result", {"tool": name, "output": output}, parent_id=self._last_tool_call_span_id)

    def on_done(self, text: str, tool_calls: Any = None) -> None:
        count = len(tool_calls) if tool_calls is not None else 0
        self._emit("run.done", {"text": text[:500], "toolCalls": count})


def tracing_events(sink: SpanSink, trace_id: Optional[str] = None) -> TracingEvents:
    return TracingEvents(sink=sink, trace_id=trace_id)