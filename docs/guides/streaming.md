# Streaming & events

Both packages expose the Run as an async iterator of typed events with independent projections — `for await` in TS, `async for` in Python.

## Event types

`StreamEvent` (both languages):

| Type | Payload | When |
|---|---|---|
| `step` | `step: number` | start of every loop iteration |
| `token` | `delta: string` | answer text delta |
| `thinking` | `delta: string` | reasoning trace delta |
| `tool_call` | `name`, `args` | before execution (after `beforeTool` hooks) |
| `tool_result` | `name`, `output` | after execution + `afterTool` hooks |
| `done` | `text` | final answer; then the `AgentResult` is available |

## TypeScript

```ts
for await (const event of agent.stream(input, { signal })) {
  if (event.type === "token") process.stdout.write(event.delta);
}
```

The `async *stream()` generator carries the final `AgentResult` as its return value — drain it fully (what `run()` does) and the last `next()` yields `{ done: true, value: AgentResult }`. `collectStream(stream)` buckets both.

`stream()` accepts `RunOptions`: `signal: AbortSignal` (checked every step **and** propagated into the in-flight provider `fetch`, so `controller.abort()` stops the HTTP request, not just the loop), `invocationState`, `checkpointer` + `threadId`, and per-run budget guards (`runWithBudget`). Abort surfaces as `AgentError("ABORTED")`.

## Python

Python async generators cannot return values, so `agent.stream(...)` returns an `AgentStream` wrapper that captures the finished `AgentResult` **per stream** at the moment the `done` event is observed (the agent records it *before* yielding `done`), exposing it as `.result`:

```python
stream = agent.stream(input_text)
async for event in stream:
    if event["type"] == "token":
        print(event["delta"], end="")
result = stream.result   # this run's AgentResult, never another concurrent run's
```

`await agent.run(...)` drains the stream and returns the result. `collect_stream(stream)` returns `{"events": [...], "result": ...}` (raises `RUN_INCOMPLETE` if the stream ended without a result).

Per-run abort/timeout guards live in `runtime.py`: `AbortHandle` (`set()` to abort, `.aborted`, `.throw_if_aborted()`) and `run_with_budget(fn, Budget(signal=asyncio.Event | None, timeout_ms=None))`, an `ABORTED`/`TIMEOUT` race. `AgentClient.stream(text, context_id, signal, on_context=...)` accepts an abort-like signal (`is_set()`/`done()` probe) so HTTP consumers can stop mid-stream.

## Delta delivery: live streaming with a batched fallback

Token and thinking deltas stream **LIVE during the model call** in both languages: as soon as the provider fires `onToken`/`onThinking`, the deltas yield to the consumer while the call is still in flight — no waiting for the call to finish. This engages whenever **no middleware declares `wrapModel` or `afterModel`**; with such middleware present, a **batched fallback** buffers the deltas and yields them only after the call settles (a `wrapModel` retry belongs to the same burst — deltas a FAILED attempt buffered are rolled back and never flush). Consequences:

- Tool events (`tool_call`, `tool_result`) yield live, mid-run — tool execution visibly interleaves with the deltas of the call that requested it.
- A `wrapModel` middleware may retry the call; the consumer sees one burst of deltas per completed attempt, never a torn mix.
- Retries never duplicate streamed text: once deltas have reached the caller, the providers stamp a mid-stream failure **non-retryable** (the delivered-verdict contract — before any delta, failures stay retryable) and the loop propagates without stamping an explicit verdict further.
- `thinking` deltas are separate events; text deltas remain in `token`.

## Streaming provider config

Providers stream only when constructed with `stream: true`:

- `initChatModel(spec, { stream: true })` (TS) / `init_chat_model(spec, stream=True)` (Python).
- With `stream: true`, the OpenAI-compatible provider sends `stream: true` plus `stream_options: { include_usage: true }` — without `stream_options`, streaming runs report `{0, 0}` usage and budgets/cost would be fiction. The usage-only final chunk (non-null `usage`, empty `choices`) is parsed from the SSE tail. Together sends `"usage": null` on content chunks; only the final chunk carries real counts.
- Anthropic and Ollama providers ship the same stream toggle with their own SSE machines; `MockModelProvider`/`ScriptedModelProvider` emit scripted events.

## Reasoning fields

Reasoning/thinking is read across provider spellings (both languages, json and SSE delta paths):

- Together: `reasoning` (message field and delta field)
- vLLM / DeepSeek / OpenAI-style: `reasoning_content`

Token counts: top-level `usage.reasoning_tokens` (Together) or nested `usage.completion_tokens_details.reasoning_tokens` (OpenAI). Reasoning counts are treated as a **subset** of output tokens everywhere (billing: [budgets](budgets.md); storage: `outputVersion: "v1"` writes a `{type:"reasoning"}` block alongside the final text turn).

## Truncation guards

The OpenAI-compatible providers enforce two loud failures (JSON and streaming paths):

- **Silent-empty guard** — `finish_reason: "length"` with zero content and no tool calls (a thinking model burned its whole `maxTokens` thinking): throws `AgentError("MODEL_ERROR", ..., retryable=false)` — "raise maxTokens".
- **Reasoning-split guard** — `finish_reason: "stop"` with empty content, no tool calls, and non-empty thinking (the whole answer landed in the reasoning channel): throws `MODEL_ERROR` **retryable** — the model/retry middleware recovers it automatically; the reply was lost in the reasoning/content split. Partial content with thinking keeps going.

## Related

- [Agents & tools](agents-and-tools.md) — dispatch order and hook timing
- [Middleware & HITL](middleware.md) — `wrapModel` retry in front of these events
- [React (frontend)](react.md) — consuming these events in the browser