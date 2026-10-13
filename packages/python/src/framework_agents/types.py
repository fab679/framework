"""Message + usage models. Parity: packages/typescript/src/types/*.

Pinned semantics (these are the FIXED ones — the python port encodes them
from day one, not the bugs the TS sweep found):

- TokenUsage.reasoning_tokens is a SUBSET of output_tokens on every supported
  provider (OpenAI reasoning_tokens C completion_tokens; Together/Anthropic
  think inside completion/output tokens). Costs/budgets NEVER add it on top.
- cost_usd is set only when a price is known — never guessed (never a fake $0).
- run()-created user turns carry no id (editMessage parity addresses by index).
"""

from __future__ import annotations

import itertools
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic import TypeAdapter

Role = Literal["system", "user", "assistant", "tool"]


class TokenUsage(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    input_tokens: int = Field(default=0, serialization_alias="inputTokens", validation_alias="inputTokens")
    output_tokens: int = Field(default=0, serialization_alias="outputTokens", validation_alias="outputTokens")
    reasoning_tokens: int | None = Field(
        default=None,
        description="SUBSET of output_tokens on every supported provider: report it, never add it",
        serialization_alias="reasoningTokens",
        validation_alias="reasoningTokens",
    )
    cost_usd: float | None = Field(
        default=None,
        description="USD cost when a price is known (see pricing). Never guessed.",
        serialization_alias="costUsd",
        validation_alias="costUsd",
    )

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            reasoning_tokens=(self.reasoning_tokens or 0) + (other.reasoning_tokens or 0),
        )


class ToolCall(BaseModel):
    id: str
    name: str
    args: Any = None


class ExecutedToolCall(BaseModel):
    id: str
    name: str
    args: Any = None
    output: Any = None


class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str


class ReasoningBlock(BaseModel):
    type: Literal["reasoning"] = "reasoning"
    reasoning: str


class ImageBlock(BaseModel):
    type: Literal["image"] = "image"
    url: str | None = None
    data: str | None = None
    mime_type: str | None = Field(default=None, serialization_alias="mimeType", validation_alias="mimeType")
    file_id: str | None = Field(default=None, serialization_alias="fileId", validation_alias="fileId")


class AudioBlock(BaseModel):
    type: Literal["audio"] = "audio"
    url: str | None = None
    data: str | None = None
    mime_type: str | None = Field(default=None, serialization_alias="mimeType", validation_alias="mimeType")
    file_id: str | None = Field(default=None, serialization_alias="fileId", validation_alias="fileId")


class FileBlock(BaseModel):
    type: Literal["file"] = "file"
    url: str | None = None
    data: str | None = None
    mime_type: str | None = Field(default=None, serialization_alias="mimeType", validation_alias="mimeType")
    file_id: str | None = Field(default=None, serialization_alias="fileId", validation_alias="fileId")


class ToolCallBlock(BaseModel):
    type: Literal["tool_call"] = "tool_call"
    id: str
    name: str
    args: Any = None


class NonStandardBlock(BaseModel):
    type: Literal["non_standard"] = "non_standard"
    value: Any = None


ContentBlock = (
    TextBlock
    | ReasoningBlock
    | ImageBlock
    | AudioBlock
    | FileBlock
    | ToolCallBlock
    | NonStandardBlock
)

_BLOCK_TYPES: dict[str, type[BaseModel]] = {
    "text": TextBlock,
    "reasoning": ReasoningBlock,
    "image": ImageBlock,
    "audio": AudioBlock,
    "file": FileBlock,
    "tool_call": ToolCallBlock,
    "non_standard": NonStandardBlock,
}


def parse_content_block(raw: Any) -> BaseModel:
    if not isinstance(raw, dict):
        # A bare string (or any other shape) in a content list used to crash
        # later text_of() work with AttributeError; fail loudly, naming it.
        raise ValueError(
            f"content list elements must be block dicts/models (got {type(raw).__name__})"
        )
    kind = raw.get("type")
    cls = _BLOCK_TYPES.get(str(kind))
    if cls is None:
        raise ValueError(f"Unknown content block type: {kind!r}")
    return cls.model_validate(raw)


# Parsed blocks are memoized per message INSTANCE (id-keyed): text_of /
# wire_text_of / providers re-parse the same content repeatedly per run.
# The entry is cleared whenever the model fields are (re)validated, and a
# new instance parses fresh (a fresh id never collides with a live entry).
_BLOCKS_CACHE: dict[int, list[BaseModel]] = {}


class ChatMessage(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str | None = None
    role: Role
    content: str | list[Any]  # string or parsed ContentBlock models (validated lazily)
    name: str | None = None
    tool_call_id: str | None = Field(default=None, serialization_alias="toolCallId", validation_alias="toolCallId")
    tool_name: str | None = Field(default=None, serialization_alias="toolName", validation_alias="toolName")
    pinned: bool | None = None

    @model_validator(mode="after")
    def _clear_block_cache(self) -> "ChatMessage":
        _BLOCKS_CACHE.pop(id(self), None)
        return self

    def blocks(self) -> list[BaseModel]:
        """Parsed content blocks (cached per instance; str content -> [])."""
        if isinstance(self.content, str):
            return []
        cached = _BLOCKS_CACHE.get(id(self))
        if cached is not None:
            return list(cached)
        parsed: list[BaseModel] = []
        for block in self.content:
            if isinstance(block, BaseModel):
                parsed.append(block)
            else:
                parsed.append(parse_content_block(block))
        _BLOCKS_CACHE[id(self)] = parsed
        return list(parsed)


_message_seq = itertools.count(1)


def system(content: str | list[Any]) -> ChatMessage:
    return ChatMessage(id=f"msg_{next(_message_seq)}", role="system", content=content)


def user(content: str | list[Any], id: str | None = None, name: str | None = None) -> ChatMessage:
    msg_id = id or f"msg_{next(_message_seq)}"
    return ChatMessage(id=msg_id, role="user", content=content, name=name)


def assistant(content: str | list[Any]) -> ChatMessage:
    return ChatMessage(id=f"msg_{next(_message_seq)}", role="assistant", content=content)


def tool_result(tool_call_id: str, tool_name: str, content: str | list[Any]) -> ChatMessage:
    return ChatMessage(
        id=f"msg_{next(_message_seq)}",
        role="tool",
        content=content,
        tool_call_id=tool_call_id,
        tool_name=tool_name,
    )


_CHAT_MESSAGE_LIST = TypeAdapter(list[ChatMessage])


def serialize_messages(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    """JSON-safe history for persistence."""
    return [m.model_dump(by_alias=True, exclude_none=True) for m in messages]


def deserialize_messages(raw: list[dict[str, Any]]) -> list[ChatMessage]:
    """Validated history restore. Raises on bad shape (never silently drops)."""
    try:
        return _CHAT_MESSAGE_LIST.validate_python(raw)
    except ValidationError as err:
        raise ValueError(f"Invalid message history: {err}") from err


def text_of(content: str | list[Any]) -> str:
    """Plain-text view of any content shape."""
    if isinstance(content, str):
        return content
    out: list[str] = []
    for b in content if isinstance(content, list) else []:
        block = b if isinstance(b, BaseModel) else parse_content_block(b)
        if isinstance(block, TextBlock):
            out.append(block.text)
        elif isinstance(block, ReasoningBlock):
            out.append(block.reasoning)
    return "".join(out)


def wire_text_of(content: str | list[Any]) -> str:
    """Text as re-sent to a provider: reasoning blocks are DROPPED
    (a model's own past thinking must not come back as ordinary text)."""
    if isinstance(content, str):
        return content
    out: list[str] = []
    for b in content if isinstance(content, list) else []:
        block = b if isinstance(b, BaseModel) else parse_content_block(b)
        if isinstance(block, TextBlock):
            out.append(block.text)
    return "".join(out)


def has_images(content: str | list[Any]) -> bool:
    return isinstance(content, list) and any(
        (b if isinstance(b, dict) else b.model_dump()).get("type") == "image"
        for b in content
    )


def to_json_shallow(value: Any) -> str:
    return json.dumps(value, default=str)