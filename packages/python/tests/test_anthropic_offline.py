"""Anthropic provider offline tests — every pinned fix from the TS port:

- tool results from ONE assistant turn grouped into a SINGLE user message
- CRLF/chunk-split SSE: event names must carry across chunk boundaries
- message_start/message_delta usage (cumulative output: assign, never add)
- stop_reason normalized (max_tokens -> length) + loud truncation guard
- thinking budget validated at construction (max_tokens must exceed it)
- reasoning never re-sent as plain text (wire_text_of semantics)
"""

from __future__ import annotations

import json
import pathlib
import sys

import httpx
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "src"))

from framework_agents import AgentError  # noqa: E402
from framework_agents.models import CompleteOptions, ToolWire  # noqa: E402
from framework_agents.models.anthropic import AnthropicOptions, AnthropicProvider  # noqa: E402
from framework_agents.types import ChatMessage, ToolCallBlock  # noqa: E402

API_KEY = "test-key"


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _messages_response(blocks: list[dict], stop: str | None = None) -> httpx.Response:
    return httpx.Response(200, json={
        "content": blocks,
        **({"stop_reason": stop} if stop else {}),
        "usage": {"input_tokens": 11, "output_tokens": 22},
    })


async def test_system_extracted_and_tools_wire_input_schema():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.read())
        return _messages_response([{"type": "text", "text": "ok"}], stop="end_turn")

    provider = AnthropicProvider(AnthropicOptions(
        api_key=API_KEY, model="claude-mock", fetch_fn=lambda: _client(handler),
    ))
    out = await provider.complete(CompleteOptions(
        messages=[ChatMessage(role="system", content="be brief"), ChatMessage(role="user", content="hi")],
        tools=[ToolWire(name="get_weather", description="w", parameters_schema={"type": "object", "properties": {"city": {"type": "string"}}})],
    ))
    body = seen["body"]
    assert body["system"] == "be brief"
    assert [m["role"] for m in body["messages"]] == ["user"]
    assert body["tools"][0]["input_schema"]["properties"]["city"]["type"] == "string"
    assert out.text == "ok"
    assert (out.usage.input_tokens, out.usage.output_tokens) == (11, 22)


async def test_consecutive_tool_results_group_into_one_user_message():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.read())
        return _messages_response([{"type": "text", "text": "done"}], stop="end_turn")

    provider = AnthropicProvider(AnthropicOptions(
        api_key=API_KEY, model="claude-mock", fetch_fn=lambda: _client(handler),
    ))
    await provider.complete(CompleteOptions(
        messages=[
            ChatMessage(role="assistant", content=[ToolCallBlock(id="c1", name="a", args={}), ToolCallBlock(id="c2", name="b", args={})]),
            ChatMessage(role="tool", content="r1", tool_call_id="c1", tool_name="a"),
            ChatMessage(role="tool", content="r2", tool_call_id="c2", tool_name="b"),
        ],
    ))
    messages = seen["body"]["messages"]
    # Two results, ONE user message (parallel tool calls stay well-formed).
    assert len(messages) == 2, messages
    assert messages[0]["role"] == "assistant"
    assert messages[1]["role"] == "user"
    kinds = [b["type"] for b in messages[1]["content"]]
    assert kinds == ["tool_result", "tool_result"]


async def test_thinking_budget_must_be_below_max_tokens():
    with pytest.raises(ValueError, match="exceed"):
        AnthropicProvider(AnthropicOptions(api_key=API_KEY, model="claude-mock", max_tokens=1024, thinking_budget=2000))


async def test_reasoning_blocks_not_resent_as_text():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.read())
        return _messages_response([{"type": "text", "text": "ok"}], stop="end_turn")

    provider = AnthropicProvider(AnthropicOptions(api_key=API_KEY, model="claude-mock", fetch_fn=lambda: _client(handler)))
    await provider.complete(CompleteOptions(
        messages=[
            ChatMessage(role="assistant", content=[
                {"type": "reasoning", "reasoning": "secret chain of thought"},
                {"type": "text", "text": "hello"},
            ]),
        ],
    ))
    content = seen["body"]["messages"][0]["content"]
    kinds = [b["type"] for b in content]
    assert "reasoning" not in kinds
    assert content == [{"type": "text", "text": "hello"}], "a model's own past thinking must not come back as plain text"


SSE = (
    "event: message_start\n"
    'data: {"message":{"usage":{"input_tokens":10}}}\n'
    "\n"
    "event: content_block_start\n"
    'data: {"content_block":{"type":"text"}}\n'
    "\n"
    "event: content_block_delta\n"
    'data: {"delta":{"type":"text_delta","text":"hi"}}\n'
    "\n"
    "event: content_block_delta\n"
    'data: {"delta":{"type":"text_delta","text":" there"}}\n'
    "\n"
    "event: content_block_stop\n"
    "data: {}\n"
    "\n"
    "event: message_delta\n"
    'data: {"usage":{"output_tokens":22},"delta":{"stop_reason":"end_turn"}}\n'
    "\n"
)


class _FakeStreamResponse:
    """Duck-type the bits _read_stream uses (status_code, aiter_text)."""

    status_code = 200

    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks

    async def aiter_text(self):
        for chunk in self._chunks:
            yield chunk.decode() if isinstance(chunk, bytes) else chunk


async def test_stream_event_state_carries_across_chunk_boundaries():
    """THE pinned regression: splitting the SSE bytes at arbitrary offsets
    must not lose event names (resetting state per chunk dropped deltas)."""
    raw = SSE.encode()
    split_at = 7  # ugly boundary: lands mid-line constantly
    provider = AnthropicProvider(AnthropicOptions(
        api_key=API_KEY, model="claude-mock", stream=True, fetch_fn=lambda: _client(lambda r: httpx.Response(200)),
    ))
    chunks = [raw[i : i + split_at] for i in range(0, len(raw), split_at)]
    out = await provider._read_stream(_FakeStreamResponse(chunks), CompleteOptions(messages=[], tools=[]))
    assert out.text == "hi there"
    assert out.usage.input_tokens == 10
    assert out.usage.output_tokens == 22
    assert out.finish_reason == "end_turn"


async def test_stream_usage_and_max_tokens_guard():
    sse = (
        'event: content_block_delta\ndata: {"delta":{"text":"x"}}\n\n'
        'event: message_delta\ndata: {"usage":{"output_tokens":50},"delta":{"stop_reason":"max_tokens"}}\n\n'
    )
    provider = AnthropicProvider(AnthropicOptions(api_key=API_KEY, model="claude-mock", stream=True, fetch_fn=lambda: _client(lambda r: httpx.Response(200))))
    out = await provider._read_stream(_FakeStreamResponse([sse]), CompleteOptions(messages=[], tools=[]))
    assert out.finish_reason == "length"  # max_tokens normalized
    assert out.usage.output_tokens == 50  # cumulative assigned, not added

    empty_sse = (
        'event: message_delta\ndata: {"usage":{"output_tokens":1024},"delta":{"stop_reason":"max_tokens"}}\n\n'
    )
    with pytest.raises(AgentError) as err:
        await provider._read_stream(_FakeStreamResponse([empty_sse]), CompleteOptions(messages=[], tools=[]))
    assert err.value.code == "MODEL_ERROR"  # loud, retriable=False


async def test_nonstream_truncation_guard():
    def handler(request: httpx.Request) -> httpx.Response:
        return _messages_response([{"type": "text", "text": ""}], stop="max_tokens")

    provider = AnthropicProvider(AnthropicOptions(api_key=API_KEY, model="claude-mock", fetch_fn=lambda: _client(handler)))
    with pytest.raises(AgentError) as err:
        await provider.complete(CompleteOptions(messages=[]))
    assert err.value.code == "MODEL_ERROR"
    assert err.value.retryable is False