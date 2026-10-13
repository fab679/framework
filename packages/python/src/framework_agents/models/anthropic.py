"""Native Anthropic Messages API provider (httpx-based, no SDK).

Parity: packages/typescript/src/models/anthropic.ts (post-sweep semantics):

- system messages extracted into the top-level `system` field
- ALL tool results from one assistant tool_use turn arrive in a SINGLE user
  message (grouping consecutive tool results — parallel calls stay well-formed)
- reasoning blocks are DROPPED when re-sending history (wire_text_of)
- constructor FAILS LOUDLY when max_tokens <= thinking_budget (API 400 otherwise)
- streaming: the `event:` state CARRIES across chunk boundaries (a data line
  landing in a different chunk than its event line must not parse as "") —
  the TS regression fixed live — and usage arrives via message_start /
  message_delta (cumulative output_tokens: assign, never add)
- stop_reason normalized (max_tokens -> length); a max_tokens stop with ZERO
  usable content raises a loud MODEL_ERROR, and partial content keeps going
- tool schemas ride `input_schema`; signals accepted for abort.

No personal/brand names in code beyond the wire protocol constants the API
itself requires (header names, paths).
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass
from typing import Any, Callable

import httpx

from ..errors import AgentError
from ..types import ChatMessage, TokenUsage, has_images, wire_text_of
from . import CompleteOptions, ModelCompletion, ModelProvider, ToolCallRequest
from .capabilities import assert_can_handle

_CALL_SEQ = itertools.count(1)


@dataclass
class AnthropicOptions:
    api_key: str
    model: str
    base_url: str | None = None
    max_tokens: int = 1024
    temperature: float | None = None
    thinking_budget: int | None = None
    stream: bool = False
    fetch_fn: Callable[[], Any] | None = None  # httpx client factory for tests


class AnthropicProvider:
    id: str
    capabilities = {"structured_output": False, "streaming": True}

    def __init__(self, opts: AnthropicOptions) -> None:
        if not opts.api_key:
            raise ValueError("AnthropicProvider: apiKey is required (.env ANTHROPIC_API_KEY)")
        self.opts = AnthropicOptions(
            base_url=opts.base_url or "https://api.anthropic.com",
            api_key=opts.api_key,
            model=opts.model,
            max_tokens=opts.max_tokens,
            temperature=opts.temperature,
            thinking_budget=opts.thinking_budget,
            stream=opts.stream,
            fetch_fn=opts.fetch_fn,
        )
        self.id = f"anthropic:{opts.model}"
        # Extended thinking requires max_tokens > budget_tokens — fail at
        # construction with the fix, not as a confusing API 400 on first call.
        if self.opts.thinking_budget is not None and self.opts.max_tokens <= self.opts.thinking_budget:
            raise ValueError(
                f"AnthropicProvider: max_tokens ({self.opts.max_tokens}) must exceed "
                f"thinking_budget ({self.opts.thinking_budget}); raise max_tokens"
            )

    def _content(self, message: ChatMessage) -> Any:
        if isinstance(message.content, str):
            return message.content
        parts: list[dict[str, Any]] = []
        text = wire_text_of(message.content)
        if text:
            parts.append({"type": "text", "text": text})
        for block in message.blocks():
            kind = block.model_dump()
            if kind["type"] == "image":
                data = kind.get("data")
                if data:
                    parts.append({"type": "image", "source": {
                        "type": "base64",
                        "media_type": kind.get("mimeType") or "image/jpeg",
                        "data": data,
                    }})
                elif kind.get("url"):
                    parts.append({"type": "image", "source": {"type": "url", "url": kind["url"]}})
            elif kind["type"] == "tool_call":
                parts.append({"type": "tool_use", "id": kind["id"], "name": kind["name"], "input": kind.get("args") or {}})
        return parts

    def _messages(self, all_messages: list[ChatMessage]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for message in all_messages:
            if message.role == "system":
                continue
            if message.role == "tool":
                # ALL tool_results for one tool_use turn belong in a SINGLE
                # user message; group consecutive tool results.
                result = {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id,
                    "content": self._content(message),
                }
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) \
                        and any(b.get("type") == "tool_result" for b in out[-1]["content"]):
                    out[-1]["content"].append(result)
                    continue
                out.append({"role": "user", "content": [result]})
                continue
            out.append({
                "role": "assistant" if message.role == "assistant" else "user",
                "content": self._content(message),
            })
        return out

    async def complete(self, options: CompleteOptions) -> ModelCompletion:
        opts = self.opts
        # Images: fail fast with MODEL_CAPABILITY when the model can't see.
        assert_can_handle(opts.model, any(has_images(m.content) for m in options.messages))
        body: dict[str, Any] = {
            "model": opts.model,
            "max_tokens": opts.max_tokens,
            "messages": self._messages(options.messages),
        }
        system = "\n\n".join(
            wire_text_of(m.content) for m in options.messages if m.role == "system"
        )
        if system:
            body["system"] = system
        if opts.temperature is not None:
            body["temperature"] = opts.temperature
        if opts.thinking_budget is not None:
            body["thinking"] = {"type": "enabled", "budget_tokens": opts.thinking_budget}
        if opts.stream:
            body["stream"] = True
        if options.tools:
            body["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.parameters_schema or {"type": "object"},
                }
                for t in options.tools
            ]

        headers = {
            "content-type": "application/json",
            "x-api-key": opts.api_key,
            "anthropic-version": "2023-06-01",
            **({"accept": "text/event-stream"} if opts.stream else {}),
        }
        client = opts.fetch_fn() if opts.fetch_fn else httpx.AsyncClient(timeout=120.0)
        try:
            res = await client.request(
                "POST",
                f"{(opts.base_url or 'https://api.anthropic.com').rstrip('/')}/v1/messages",
                headers=headers,
                json=body,
            )
        except Exception as err:
            raise AgentError("MODEL_ERROR", f"Model request failed: {err}", retryable=True) from err
        finally:
            if opts.fetch_fn is None:
                await client.aclose()

        if res.status_code == 429 or res.status_code >= 500:
            raise AgentError("MODEL_ERROR", f"Model HTTP {res.status_code}: {res.text[:300]}", retryable=True)
        if res.status_code >= 400:
            raise AgentError("MODEL_ERROR", f"Model HTTP {res.status_code}: {res.text[:300]}", retryable=False)

        if opts.stream:
            return await self._read_stream(res, options)
        return self._read_json(res, options)

    def _read_json(self, res: Any, options: CompleteOptions) -> ModelCompletion:
        data = res.json()
        text = ""
        thinking = ""
        tool_calls: list[ToolCallRequest] = []
        for block in data.get("content") or []:
            if block.get("type") == "text" and block.get("text"):
                text += block["text"]
                options.on_token and options.on_token(block["text"])
            elif block.get("type") == "thinking" and block.get("thinking"):
                thinking += block["thinking"]
                options.on_thinking and options.on_thinking(block["thinking"])
            elif block.get("type") == "tool_use" and block.get("name"):
                tool_calls.append(ToolCallRequest(
                    id=block.get("id") or f"call_{next(_CALL_SEQ)}",
                    name=block["name"],
                    args=block.get("input") or {},
                ))
        normalized = _normalize_stop(data.get("stop_reason"))
        if not text and not tool_calls and thinking and normalized == "stop":
            # Empty text + non-empty thinking + stop: the reasoning/content
            # split put the WHOLE answer in the thinking channel. Never
            # return an empty answer for a thinking model's short reply.
            raise AgentError(
                "MODEL_ERROR",
                "Model stopped with stop_reason=stop having put the entire answer in "
                "the reasoning channel (empty content, no tool calls, thinking present). "
                "The reasoning/content split lost the reply — retry or raise max_tokens.",
                retryable=True,
            )
        if not text and not tool_calls and normalized == "length":
            raise AgentError(
                "MODEL_ERROR",
                f"Model stopped at max_tokens without producing content. Raise max_tokens.",
                retryable=False,
            )
        usage = data.get("usage") or {}
        return ModelCompletion(
            text=text,
            thinking=thinking or None,
            tool_calls=tool_calls,
            finish_reason=normalized,
            usage=TokenUsage(input_tokens=usage.get("input_tokens", 0), output_tokens=usage.get("output_tokens", 0)),
        )

    async def _read_stream(self, res: Any, options: CompleteOptions) -> ModelCompletion:
        text = ""
        thinking = ""
        input_tokens = 0
        output_tokens = 0
        stop_reason: str | None = None
        blocks: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        json_input = ""
        buffer = ""
        event = ""
        # Retry-verdict rule: once a delta (text/thinking/tool fragment)
        # reached the caller, a mid-stream failure is NON-retryable (re-running
        # would duplicate already-streamed text); before any delta, failures
        # stay retryable (nothing was delivered).
        delivered = False
        try:
            async for chunk in res.aiter_text():
                buffer += chunk
                lines = buffer.split("\n")
                buffer = lines.pop()
                for line in lines:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    if stripped.startswith("event:"):
                        # CRITICAL: the event name must carry across chunk
                        # boundaries — re-initializing per chunk silently drops
                        # every delta whose event line landed in a prior packet.
                        event = stripped[6:].strip()
                        continue
                    if not stripped.startswith("data:"):
                        continue
                    payload = stripped[5:].strip()
                    if not payload:
                        continue
                    try:
                        data = json.loads(payload)
                    except ValueError:
                        continue
                    if event == "message_start":
                        # message_start carries the input-side usage up front.
                        message = data.get("message") or {}
                        usage = message.get("usage") or {}
                        if usage.get("input_tokens") is not None:
                            input_tokens = usage["input_tokens"]
                    elif event == "message_delta":
                        # message_delta.usage.output_tokens is cumulative — assign.
                        usage = data.get("usage") or {}
                        if usage.get("output_tokens") is not None:
                            output_tokens = usage["output_tokens"]
                        delta = data.get("delta") or {}
                        if delta.get("stop_reason"):
                            stop_reason = _normalize_stop(delta["stop_reason"])
                    elif event == "content_block_start":
                        block = data.get("content_block") or {}
                        current = {"type": block.get("type"), "id": block.get("id"), "name": block.get("name")}
                        json_input = ""
                    elif event == "content_block_delta":
                        delta = data.get("delta") or {}
                        if delta.get("text"):
                            text += delta["text"]
                            delivered = True
                            current = current or {"type": "text"}
                            current["text"] = (current.get("text") or "") + delta["text"]
                            options.on_token and options.on_token(delta["text"])
                        elif delta.get("thinking"):
                            thinking += delta["thinking"]
                            # Thinking deltas reach the caller like text deltas
                            # do — the delivered verdict covers them (and the
                            # tool fragment below).
                            delivered = True
                            current = current or {"type": "thinking"}
                            current["thinking"] = (current.get("thinking") or "") + delta["thinking"]
                            options.on_thinking and options.on_thinking(delta["thinking"])
                        elif delta.get("partial_json"):
                            # Tool-call fragments are delivered state too: a
                            # retry would re-issue a call mid-assembly, so the
                            # attempt counts as spent.
                            delivered = True
                            json_input += delta["partial_json"]
                    elif event == "content_block_stop":
                        if current is not None:
                            if current.get("type") == "tool_use":
                                try:
                                    current["input"] = json.loads(json_input or "{}")
                                except ValueError:
                                    current["input"] = {}
                            blocks.append(current)
                            current = None
        except Exception as err:
            # Delivered-aware verdict: deltas already streamed => non-retryable
            # (a retry would duplicate the delivered text); before any delta
            # the failure stays retryable.
            raise AgentError(
                "MODEL_ERROR",
                f"Stream broke mid-response{' (deltas already delivered)' if delivered else ''}: {err}",
                retryable=not delivered,
            ) from err
        if current is not None:
            blocks.append(current)

        tool_calls = [
            ToolCallRequest(id=b.get("id") or f"call_{next(_CALL_SEQ)}", name=b["name"], args=b.get("input") or {})
            for b in blocks if b.get("type") == "tool_use" and b.get("name")
        ]
        normalized = stop_reason
        if not text and not tool_calls and thinking and normalized == "stop":
            # Stream mirror of the readJson reasoning-split guard (above).
            raise AgentError(
                "MODEL_ERROR",
                "Stream ended at stop_reason=stop having put the entire answer in "
                "the reasoning channel (no content, no tool calls, thinking present). "
                "The reasoning/content split lost the reply — retry or raise max_tokens.",
                retryable=True,
            )
        if not text and not tool_calls and normalized == "length":
            raise AgentError(
                "MODEL_ERROR",
                "Stream stopped at max_tokens without producing content. Raise max_tokens.",
                retryable=False,
            )
        return ModelCompletion(
            text=text,
            thinking=thinking or None,
            tool_calls=tool_calls,
            finish_reason=normalized,
            usage=TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
        )


def _normalize_stop(stop: str | None) -> str | None:
    if not stop:
        return None
    return "length" if stop == "max_tokens" else stop