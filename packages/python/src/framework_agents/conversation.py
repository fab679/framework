"""Conversation managers keep history within context limits.

Parity: src/conversation/index.ts.

- apply(): after-run management (before memory.save).
- reduce(): explicit shrink (overflow recovery / proactive triggers).
- before_model(): per-turn management inside the loop (sliding window).
Pinned messages are never removed. Tool/assistant-tool_call pairs are
never orphaned: trimming drops leading dangling messages.
"""

from __future__ import annotations

import inspect
import math
import re
from typing import Any, Callable, Protocol

from .models import CompleteOptions, ModelProvider
from .types import ChatMessage, TextBlock, ToolCallBlock, text_of

Summarizer = Callable[[list[ChatMessage]], Any]  # str or awaitable str

# Word-ish runs, numbers, single CJK chars, punctuation clusters — close to
# subword behavior on prose (calibrated so estimate_tokens("abcd") == 1).
_TOKEN_RE = re.compile(
    r"[A-Za-z]+(?:'[A-Za-z]+)?|\d+(?:\.\d+)?|[一-鿿぀-ヿ가-힯]|[^A-Za-z\d\s]"
)
_WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?$")
_CJK_RE = re.compile(r"[一-鿿぀-ヿ가-힯]")


def estimate_tokens(text: str) -> int:
    """Local token estimate — dependency-free approximation, not a BPE."""
    if not text:
        return 0
    tokens = 0
    for w in _TOKEN_RE.findall(text):
        if _CJK_RE.fullmatch(w):
            tokens += 1
        elif _WORD_RE.fullmatch(w):
            tokens += max(1, math.ceil(len(w) / 5))
        else:
            tokens += 1
    return tokens


class ConversationManager(Protocol):
    """History-shrinking hook set. removed_count tracks load skipped so far."""

    name: str
    removed_count: int

    async def apply(self, messages: list[ChatMessage]) -> list[ChatMessage]: ...
    async def reduce(self, messages: list[ChatMessage]) -> list[ChatMessage]: ...


def _has_only_tool_calls(content: Any) -> bool:
    if not isinstance(content, list):
        return False
    blocks = [
        b
        for b in content
        if (b.get("type") if isinstance(b, dict) else b.type) == "tool_call"
        or (
            (b.get("type") if isinstance(b, dict) else b.type) == "text"
            and ((b.get("text") if isinstance(b, dict) else b.text) or "").strip() == ""
        )
    ]
    return len(blocks) > 0 and len(blocks) == len(content)


def trim_dangling(messages: list[ChatMessage]) -> list[ChatMessage]:
    """Drop leading messages that lost their pair (tool head, call-only
    assistant head). Pinned messages stop the scan: they are keepers, not
    orphans."""
    i = 0
    while i < len(messages):
        m = messages[i]
        if m.pinned:
            break
        if m.role == "tool":
            i += 1
            continue
        if m.role == "assistant" and _has_only_tool_calls(m.content):
            i += 1
            continue
        break
    return messages[i:]


def message_tokens(m: ChatMessage) -> int:
    text = m.content if isinstance(m.content, str) else text_of(m.content)
    return estimate_tokens(text)


def history_tokens(messages: list[ChatMessage]) -> int:
    return sum(message_tokens(m) for m in messages)


class NullConversationManager:
    """Keeps full history. Pass explicitly to disable the default window."""

    name = "null"
    removed_count = 0

    async def apply(self, messages: list[ChatMessage]) -> list[ChatMessage]:
        return list(messages)

    async def reduce(self, messages: list[ChatMessage]) -> list[ChatMessage]:
        return list(messages)

    async def before_model(self, messages: list[ChatMessage], step: int) -> list[ChatMessage]:
        return list(messages)


class SlidingWindowConversationManager:
    """Keep the newest `window_size` unpinned messages; pinned survive."""

    name = "sliding-window"

    def __init__(
        self,
        window_size: int = 40,
        pin_first: int = 0,
        per_turn: bool | int = False,
    ) -> None:
        self.window_size = max(1, window_size)
        self.pin_first = max(0, pin_first)
        self.per_turn = per_turn
        self.removed_count = 0

    @staticmethod
    def _pin(messages: list[ChatMessage], pin_first: int) -> None:
        for m in messages[: min(pin_first, len(messages))]:
            m.pinned = True

    def _trim(self, messages: list[ChatMessage]) -> list[ChatMessage]:
        self._pin(messages, self.pin_first)
        keep: set[int] = set()
        rest: list[int] = []
        for idx, m in enumerate(messages):
            if m.pinned:
                keep.add(idx)
            else:
                rest.append(idx)
        dropped = max(0, len(rest) - self.window_size)
        self.removed_count += dropped
        keep.update(rest[dropped:])
        # Merge in ORIGINAL order: hoisting pinned messages to the front would
        # reorder the conversation whenever a mid-history message is pinned.
        return trim_dangling([m for i, m in enumerate(messages) if i in keep])

    async def apply(self, messages: list[ChatMessage]) -> list[ChatMessage]:
        self._pin(messages, self.pin_first)
        return self._trim(messages)

    async def reduce(self, messages: list[ChatMessage]) -> list[ChatMessage]:
        return self._trim(list(messages))

    async def before_model(self, messages: list[ChatMessage], step: int) -> list[ChatMessage]:
        if self.per_turn is False:
            return messages
        if isinstance(self.per_turn, int) and (step % self.per_turn != 0 or self.per_turn <= 0):
            return messages
        return self._trim(list(messages))


class SummarizingConversationManager:
    """Summarize the oldest fraction away into a single boundary message."""

    name = "summarizing"

    def __init__(
        self,
        summary_ratio: float = 0.3,
        preserve_recent: int = 10,
        pin_first: int = 0,
        summarizer: Summarizer | None = None,
        model: ModelProvider | None = None,
        summary_prefix: str = "Summary of earlier conversation:",
    ) -> None:
        self.ratio = min(0.8, max(0.1, summary_ratio))
        self.preserve_recent = max(1, preserve_recent)
        self.pin_first = max(0, pin_first)
        self.summarizer = summarizer
        self.model = model
        self.summary_prefix = summary_prefix
        self.removed_count = 0
        # Set when the last apply() summarized (tests/UI can assert on it).
        self.last_summary: str | None = None

    def _summarizable(self, messages: list[ChatMessage]) -> tuple[list[ChatMessage], list[ChatMessage]]:
        self._pin(messages)
        eligible_idx = [i for i, m in enumerate(messages) if not m.pinned]
        count = min(
            len(eligible_idx) - self.preserve_recent,
            math.floor(len(eligible_idx) * self.ratio),
        )
        if count <= 0:
            return [], list(messages)
        cutoff = set(eligible_idx[:count])
        head = [m for i, m in enumerate(messages) if i in cutoff]
        tail = [m for i, m in enumerate(messages) if i not in cutoff]
        return head, tail

    def _pin(self, messages: list[ChatMessage]) -> None:
        SlidingWindowConversationManager._pin(messages, self.pin_first)

    def _transcript(self, messages: list[ChatMessage]) -> str:
        return "\n".join(
            f"[{m.role}] {m.content if isinstance(m.content, str) else text_of(m.content)}"
            for m in messages
        )

    async def _summarize(self, head: list[ChatMessage]) -> str:
        if self.summarizer is not None:
            result = self.summarizer(head)
            return await result if inspect.isawaitable(result) else result
        if self.model is None:
            # Extractive stub: first 200 chars of each message.
            return "\n".join(
                f"[{m.role}] {(m.content if isinstance(m.content, str) else text_of(m.content))[:200]}"
                for m in head
            )
        completion = await self.model.complete(
            CompleteOptions(
                messages=[
                    ChatMessage(
                        role="user",
                        content=f"Summarize concisely, preserving decisions, names, tool outcomes:\n{self._transcript(head)}",
                    )
                ],
                tools=[],
            )
        )
        return completion.text

    async def apply(self, messages: list[ChatMessage]) -> list[ChatMessage]:
        head, tail = self._summarizable(messages)
        if not head:
            return list(messages)
        summary = await self._summarize(head)
        self.last_summary = summary
        self.removed_count += len(head) - 1
        return [ChatMessage(role="assistant", content=f"{self.summary_prefix}\n{summary}"), *trim_dangling(tail)]

    async def reduce(self, messages: list[ChatMessage]) -> list[ChatMessage]:
        return list(messages)