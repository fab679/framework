"""Compaction: keep long runs inside the context window without losing meaning.

Parity: src/memory/compaction.ts.

Strategy (P1): oldest messages -> model/pluggable summarizer -> single
boundary message.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable

from .memory import Memory
from .types import ChatMessage, text_of

SummarizeFn = Callable[[list[ChatMessage]], Any]  # str or awaitable str


def default_summarize(messages: list[ChatMessage]) -> str:
    """Extractive stub: first five non-system messages, 200 chars each."""
    head = "\n".join(
        f"[{m.role}] {text_of(m.content)[:200]}"
        for m in [m for m in messages if m.role != "system"][:5]
    )
    return f"Prior context summary ({len(messages)} msgs):\n{head}"


class CompactedMemory:
    """Wrap any Memory with auto-compaction on save."""

    def __init__(
        self,
        inner: Memory,
        max_messages: int = 40,
        summarize: SummarizeFn | None = None,
    ) -> None:
        self.inner = inner
        self.max_messages = max_messages
        self.summarize = summarize

    async def load(self) -> list[ChatMessage]:
        return await self.inner.load()

    async def save(self, messages: list[ChatMessage]) -> None:
        await self.inner.save(messages)
        max = self.max_messages
        current = await self.inner.load()
        if len(current) <= max:
            return
        summarize = self.summarize or default_summarize
        # Overflow is replaced by ONE summary message so the stored history
        # shrinks back to exactly `max` (1 summary + max-1 newest).
        overflow = current[: len(current) - max + 1]
        rest = current[len(current) - max + 1 :]
        summary = summarize(overflow)
        if inspect.isawaitable(summary):
            summary = await summary
        await self.inner.clear()
        await self.inner.save([ChatMessage(role="assistant", content=summary), *rest])

    async def clear(self) -> None:
        await self.inner.clear()