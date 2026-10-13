"""OpenAI-compatible chat provider (TogetherAI/OpenAI/vLLM), httpx-based.

Ports the live-verified semantics from the TS provider:
- UNION field parsing: Together sends `message.reasoning` + top-level
  `usage.reasoning_tokens`; OpenAI/vLLM send `reasoning_content` +
  `completion_tokens_details.reasoning_tokens`. Both shapes work.
- Streaming: `stream_options: include_usage` requested; `"usage": null`
  content chunks skipped; usage-only final chunk parsed.
- Silent-truncation guard: finish_reason="length" with ZERO content and no
  tool calls raises a LOUD MODEL_ERROR (raise max_tokens) — never a silent
  empty answer. Partial content keeps going, with finish_reason attached.
- Tool parameter schemas go on the wire (`function.parameters`).
- Streaming tool-call ids never repeat across turns (call_<seq>).
- abort + HTTP error mapping: network/429/5xx retryable, others not.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from dataclasses import dataclass
from typing import Any, Callable

import httpx
from pydantic import BaseModel

from ..errors import AgentError
from ..types import ChatMessage, ImageBlock, TokenUsage, ToolCallBlock, has_images, wire_text_of
from . import CompleteOptions, ModelCompletion, ModelProvider, ToolCallRequest, ToolWire
from .capabilities import assert_can_handle

_CALL_SEQ = itertools.count(1)


class _Abort(Exception):
    """Internal: the caller's signal fired mid-request."""


async def _respect_signal(signal: Any) -> None:
    """No-op unless the caller passed an abort Event; wait-free when None."""
    if signal is None:
        return
    try:
        already = signal.is_set()
    except AttributeError:
        return  # foreign signal types are ignored (documented)
    if already:
        raise _Abort()


@dataclass
class OpenAICompatibleOptions:
    base_url: str
    api_key: str
    model: str
    temperature: float = 0.2
    max_tokens: int | None = None
    stream: bool = False
    fetch_fn: Callable[..., Any] | None = None  # httpx-client factory for tests


class OpenAICompatibleProvider:
    id: str
    capabilities = {"structured_output": True, "streaming": True}

    def __init__(self, opts: OpenAICompatibleOptions) -> None:
        if not opts.api_key:
            raise ValueError("OpenAICompatibleProvider: apiKey is required")
        self.id = f"openai-compatible:{opts.model}"
        self.opts = opts

    # -- wire conversion ---------------------------------------------------

    def _wire_content(self, message: ChatMessage, blocks: list[BaseModel]) -> Any:
        if isinstance(message.content, str):
            return message.content
        text = wire_text_of(message.content)
        images = [b for b in blocks if isinstance(b, ImageBlock)]
        if not images:
            return text
        parts: list[dict[str, Any]] = ([{"type": "text", "text": text}] if text else [])
        for img in images:
            data = getattr(img, "data", None)
            mime = getattr(img, "mime_type", None) or "image/jpeg"
            url = f"data:{mime};base64,{data}" if data else getattr(img, "url", None)
            parts.append({"type": "image_url", "image_url": {"url": url}})
        return parts

    def _wire_message(self, message: ChatMessage) -> dict[str, Any]:
        # blocks() is parsed ONCE per message (memoized at the types layer)
        # and reused for both the content and the tool_calls halves.
        blocks: list[BaseModel] = [] if isinstance(message.content, str) else message.blocks()
        base: dict[str, Any] = {
            "role": message.role,
            "content": self._wire_content(message, blocks),
        }
        if message.tool_call_id:
            base["tool_call_id"] = message.tool_call_id
        if blocks:
            calls = [b for b in blocks if isinstance(b, ToolCallBlock)]
            if calls:
                base["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {"name": c.name, "arguments": json.dumps(c.args or {})},
                    }
                    for c in calls
                ]
        return base

    # -- request -----------------------------------------------------------

    async def complete(self, options: CompleteOptions) -> ModelCompletion:
        opts = self.opts
        # Images: fail fast with MODEL_CAPABILITY when the model can't see.
        assert_can_handle(opts.model, any(has_images(m.content) for m in options.messages))
        tools = options.tools
        body: dict[str, Any] = {
            "model": opts.model,
            "temperature": opts.temperature,
            "messages": [self._wire_message(m) for m in options.messages],
            **({"max_tokens": opts.max_tokens} if opts.max_tokens else {}),
            # stream_options makes OpenAI-compatible servers attach usage to
            # the final chunk; without it streaming runs report 0 tokens.
            **(
                {"stream": True, "stream_options": {"include_usage": True}}
                if opts.stream
                else {}
            ),
        }
        if options.response_format:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": options.response_format.name,
                    "strict": options.response_format.strict if options.response_format.strict is not None else True,
                    "schema": options.response_format.schema,
                },
            }
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        # Without parameters the API advertises a zero-arg tool.
                        **({"parameters": t.parameters_schema} if t.parameters_schema else {}),
                    },
                }
                for t in tools
            ]

        client = opts.fetch_fn() if opts.fetch_fn else httpx.AsyncClient(timeout=120.0)
        url = f"{opts.base_url.rstrip('/')}/chat/completions"
        headers = {"content-type": "application/json", "authorization": f"Bearer {opts.api_key}"}
        try:
            # Stream=True NEVER lets httpx buffer the whole body first: the
            # request must consume incrementally so on_token/on_thinking fire
            # live and abort can actually interrupt the upload/response.
            # Duck-typed test clients exposing only .request() fall back
            # (they already return a fully-formed response object).
            if hasattr(client, "build_request"):
                request = client.build_request("POST", url, headers=headers, json=body)
                res = await client.send(request, stream=opts.stream)
            else:
                res = await client.request("POST", url, headers=headers, json=body)

            # Abort actually CANCELS the in-flight request (the old code took
            # options.signal and ignored it — abort ran to completion and burned
            # tokens). Race the response wait against the caller's signal.
            await _respect_signal(options.signal)

            if res.status_code == 429 or res.status_code >= 500:
                text = (await res.aread()).decode("utf-8", "replace") if opts.stream else res.text
                raise AgentError("MODEL_ERROR", f"Model HTTP {res.status_code}: {text[:300]}", retryable=True)
            if res.status_code >= 400:
                text = (await res.aread()).decode("utf-8", "replace") if opts.stream else res.text
                raise AgentError("MODEL_ERROR", f"Model HTTP {res.status_code}: {text[:300]}", retryable=False)

            if opts.stream:
                try:
                    # Live consumption — the client stays open until the stream ends.
                    return await self._read_stream(res, options)
                finally:
                    await res.aclose()
            return self._read_json(res, options)
        except _Abort:
            raise AgentError("ABORTED", "Run aborted", retryable=False)
        except Exception as err:
            if isinstance(err, AgentError):
                raise
            raise AgentError("MODEL_ERROR", f"Model request failed: {err}", retryable=True) from err
        finally:
            if opts.fetch_fn is None:
                await client.aclose()

    def _read_json(self, res: Any, options: CompleteOptions) -> ModelCompletion:
        data = res.json()
        choices = data.get("choices") or [{}]
        choice = choices[0] or {}
        message = choice.get("message") or {}
        thinking = _thinking_of(message)
        if thinking:
            options.on_thinking and options.on_thinking(thinking)
        content = message.get("content") or ""
        if content:
            options.on_token and options.on_token(content)
        tool_calls = [
            ToolCallRequest(
                id=(c.get("id") or f"call_{next(_CALL_SEQ)}"),
                name=(c.get("function") or {}).get("name", ""),
                args=_safe_json((c.get("function") or {}).get("arguments", "{}")),
            )
            for c in (message.get("tool_calls") or [])
        ]
        usage = data.get("usage") or {}
        finish_reason = choice.get("finish_reason") or None
        reasoning = _reasoning_of(usage)
        if not content and not tool_calls and thinking and finish_reason == "stop":
            # Empty content + non-empty thinking + stop: the reasoning/content
            # split put the WHOLE answer in the reasoning channel (thinking
            # models do this on short replies). Never return an empty answer.
            raise AgentError(
                "MODEL_ERROR",
                "Model stopped with finish_reason=stop having put the entire answer in "
                "the reasoning channel (empty content, no tool calls, thinking present). "
                "The reasoning/content split lost the reply — retry or raise maxTokens.",
                retryable=True,
            )
        if not content and not tool_calls and finish_reason == "length":
            # A thinking model can burn its WHOLE max_tokens before any
            # content: never let that come back as a successful empty reply.
            raise AgentError(
                "MODEL_ERROR",
                f"Model spent its entire max_tokens budget ({usage.get('completion_tokens', '?')} tokens, "
                "finish_reason=length) thinking without producing content. Raise maxTokens.",
                retryable=False,
            )
        return ModelCompletion(
            text=content,
            thinking=thinking,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=TokenUsage(
                input_tokens=usage.get("prompt_tokens", 0),
                output_tokens=usage.get("completion_tokens", 0),
                reasoning_tokens=reasoning,
            ),
        )

    async def _read_stream(self, res: Any, options: CompleteOptions) -> ModelCompletion:
        text = ""
        thinking = ""
        input_tokens = 0
        output_tokens = 0
        reasoning_tokens: int | None = None
        finish_reason: str | None = None
        tool_buffers: dict[int, dict[str, str]] = {}

        # Retry-verdict rule: once a delta (content/thinking/tool fragment)
        # reached the caller, a mid-stream failure is NON-retryable (re-running
        # would duplicate already-streamed text); before any delta, failures
        # stay retryable (nothing was delivered).
        delivered = False

        # A line can SPLIT across TCP reads: the first fragment would fail
        # json.loads and the continuation would not start with "data:" — both
        # silently dropped. Accumulate a buffer and only parse COMPLETE
        # lines (TS parity: buffer += chunk; lines.pop()).
        buffer = ""

        # Mid-STREAM abort: closing the response interrupts aiter_text, the
        # same way TS aborts via the fetch signal.
        watchdog: "asyncio.Task[None] | None" = None
        if options.signal is not None:
            async def _watch() -> None:
                await options.signal.wait()  # type: ignore[union-attr]
                await res.aclose()

            watchdog = asyncio.create_task(_watch())

        try:
            async for chunk in res.aiter_text():
                buffer += chunk
                lines = buffer.split("\n")
                buffer = lines.pop()
                for line in lines:
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        continue
                    try:
                        evt = json.loads(payload)
                    except ValueError:
                        continue
                    first = (evt.get("choices") or [{}])[0] or {}
                    if first.get("finish_reason"):
                        finish_reason = first["finish_reason"]
                    usage = evt.get("usage")
                    # Together sends "usage": null on content chunks; the final
                    # usage-only chunk has an empty/partial choices array.
                    if usage:
                        input_tokens = usage.get("prompt_tokens", input_tokens)
                        output_tokens = usage.get("completion_tokens", output_tokens)
                        reasoning_tokens = _reasoning_of(usage)
                        continue
                    delta = first.get("delta") or {}
                    think = _thinking_of(delta)
                    if think:
                        thinking += think
                        # Thinking deltas reach the caller like token deltas
                        # do — the delivered verdict covers them (and tool
                        # fragments below).
                        delivered = True
                        options.on_thinking and options.on_thinking(think)
                    if delta.get("content"):
                        text += delta["content"]
                        delivered = True
                        options.on_token and options.on_token(delta["content"])
                    for i, tc in enumerate(delta.get("tool_calls") or []):
                        key = tc.get("index", i)
                        # Tool-call fragments are delivered state too: a retry
                        # would re-issue a call mid-assembly, so treat the
                        # attempt as spent.
                        delivered = True
                        slot = tool_buffers.setdefault(key, {"id": "", "name": "", "args": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["args"] += fn["arguments"]
        except Exception as err:
            # The run's own signal fired mid-request (abort): report ABORTED,
            # never a stream error.
            probe = getattr(options.signal, "is_set", None)
            if callable(probe) and probe():
                raise AgentError("ABORTED", "Run aborted", retryable=False) from err
            # Delivered-aware verdict: deltas already streamed => non-retryable
            # (a retry would duplicate the delivered text); before any delta
            # the failure stays retryable.
            raise AgentError(
                "MODEL_ERROR",
                f"Stream broke mid-response{' (deltas already delivered)' if delivered else ''}: {err}",
                retryable=not delivered,
            ) from err
        finally:
            if watchdog is not None:
                watchdog.cancel()

        if not text and not tool_buffers and thinking and finish_reason == "stop":
            # Stream mirror of the readJson reasoning-split guard (above).
            raise AgentError(
                "MODEL_ERROR",
                "Stream ended at finish_reason=stop having put the entire answer in "
                "the reasoning channel (no content, no tool calls, thinking present). "
                "The reasoning/content split lost the reply — retry or raise maxTokens.",
                retryable=True,
            )
        if not text and not tool_buffers and finish_reason == "length":
            raise AgentError(
                "MODEL_ERROR",
                f"Stream ended at finish_reason=length with no content ({output_tokens} tokens burned). Raise maxTokens.",
                retryable=False,
            )
        tool_calls = [
            ToolCallRequest(
                id=(t["id"] or f"call_{next(_CALL_SEQ)}"),
                name=t["name"],
                args=_safe_json(t["args"]),
            )
            for t in tool_buffers.values()
            if t["name"]
        ]
        return ModelCompletion(
            text=text,
            thinking=thinking or None,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=TokenUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                reasoning_tokens=reasoning_tokens,
            ),
        )


def _thinking_of(message: dict[str, Any]) -> str | None:
    trace = message.get("reasoning") or message.get("reasoning_content")
    return trace if trace else None


def _reasoning_of(usage: dict[str, Any]) -> int | None:
    # Together reports top-level, OpenAI nests in completion_tokens_details.
    value = usage.get("reasoning_tokens")
    if value is None:
        details = usage.get("completion_tokens_details") or {}
        value = details.get("reasoning_tokens")
    return value


def _safe_json(raw: str) -> Any:
    try:
        return json.loads(raw or "{}")
    except ValueError:
        return {}


def provider_from_env(
    active: dict[str, str],
    extra: dict[str, Any] | None = None,
) -> OpenAICompatibleProvider:
    """Build a provider from env config (TogetherAI preferred when key present).

    TS naming mapped to the python options: ``active`` carries the camelCase
    env keys (``baseUrl``/``apiKey``/``model``; snake_case accepted as a
    fallback); ``extra`` is any extra OpenAICompatibleOptions kwargs
    (temperature, maxTokens, stream, fetchFn).
    """
    base_url = active.get("baseUrl") or active.get("base_url")
    api_key = active.get("apiKey") or active.get("api_key")
    model = active.get("model")
    if not base_url or not api_key or not model:
        raise ValueError("providerFromEnv: active config requires baseUrl, apiKey and model")
    extra_mapped = dict(extra or {})
    if "maxTokens" in extra_mapped:
        extra_mapped.setdefault("max_tokens", extra_mapped.pop("maxTokens"))
    if "fetchFn" in extra_mapped:
        extra_mapped.setdefault("fetch_fn", extra_mapped.pop("fetchFn"))
    return OpenAICompatibleProvider(
        OpenAICompatibleOptions(base_url=base_url, api_key=api_key, model=model, **extra_mapped)
    )