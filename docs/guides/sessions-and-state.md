# Sessions, threads & state — what lives where

This is the mental model for state when a React (or any SSE) frontend talks to
a served agent. The one rule to remember: **the client owns the wire; the
server owns the memory.**

```txt
UI (useAgent)                       wire (SSE)                       Server (A2A / serve)
─────────────                       ─────────                        ─────────────────────
turns, thinking, activity  ◄─────── {contextId, event} ◄─────────  per-context Agent (LRU cache)
status, error, SendResult  ──────►  POST {message, contextId} ───►  ├─ memory: clear-then-save per run
contextRef.current (id only)  ◄────  {contextId, done, text} ◄────  ├─ session: SessionManager(name:ctxId)
                                                                    ├─ checkpoints: FileCheckpointer(ctxId)
 AgentState / invocationState / swarm sharedState — server only, never serialized to UI or model
```

## 1. Sessions and threads: the client only carries an id

- On the first send, the server mints `contextId` (`ctx-<hex36>`) unless you
  supplied one. Every SSE frame carries it; the hook's `onContext` captures it
  (`send.to` also returns theSend-result's contextId).
- Every subsequent `send()` posts it back as `params.contextId`. That is the
  entire client-side "session": an opaque handle.
- What the id selects **server-side** (`A2AServer` / `serveFramework`):
  - **Per-context agent** — `agentFactory(contextId)` result cached in an LRU
    (`maxContexts`, default 1000). Same id ⇒ same agent instance ⇒ its
    memory/history continues the conversation. Never reuse one instance
    across contexts (that would leak user A's history into user B's).
  - **Session persistence** — with `sessionDir` in framework.json, each
    context agent gets `SessionManager(session_id = "<def-name>:<contextId>",
    storage: LocalFileStorage)`. History + `AgentState` restore on the first
    run and save after every run: the conversation survives server restarts
    while the UI holds no history at all.
- **ThreadId** = the same `contextId` when a `checkpointer` is configured:
  checkpointed HITL (suspend → human decision → resume) is keyed per context;
  the server side of "pause" is a thread.
- Want the conversation reset? Skip `contextId` (or call `reset()`), and the
  server mints a fresh one. Want a resumable conversation later? Persist
  `contextId` (e.g. localStorage keyed by user) and pass it via
  `useAgent({ contextId })`.

## 2. The three server-side state lifetimes (never cross the wire)

| Lifetime | Lives in | Shared across | Serialized to model? | Serialized to UI? |
|---|---|---|---|---|
| Conversation history | agent memory / runLoop messages | steps in one run + turns of the session | yes (as messages) | no — only via `token`/`done` events |
| `AgentState` (kv) | `Agent` instance | the whole agent lifetime | never | no |
| `invocationState` (per-run) | passed between hooks/tools by reference | one run | never | no |
| swarm `sharedState` | swarm bookkeeping | members of one topology | never | no |

`invocationState` is deliberately by-reference server-side: tools and hooks
read/write the same object mid-run (build resolvers, progress trackers,
rate-limit buckets). Because the loop's model calls only receive *messages*,
and the SSE stream only emits *events*, none of this escapes the server. If
you need server→UI values mid-run today, surface them as **tool calls** —
`tool_call`/`tool_result` events cross the wire and appear in `activity`
(names) and the `SendResult.toolCalls` list.

## 3. What the UI can actually see

| UI field | Source event | Server counterpart |
|---|---|---|
| `turns` (text deltas / final reconcile) | `token` / `done` | completion text per run |
| `thinking` | `thinking` | reasoning deltas |
| `activity` (name lines) | `tool_call` / `tool_result` | tool executions in the run loop |
| `status` / `error` | stream lifecycle | run started/finished/failed |
| `contextId` | every frame's `contextId` | the session/thread key |

`await send(text)` additionally resolves with `{ text, toolCalls, contextId }`
— the awaited final text and the tools used in that run (names, in call
order — outputs live in server-side `AgentResult.toolCalls` records).

## 4. Recipes

**Per-user conversations** — key `contextId` by user: pass
`useAgent({ contextId: userId })` (or your own `ctx-<userId>` scheme); the
server isolates agent + session + checkpoints per id automatically.

**Server restarts** — with `checkpointer.dir` set, suspended HITL runs resume
from disk; with `sessionDir`, finished runs restore their history + state.
The UI does nothing special: keep sending the stored `contextId`.

**Inspecting server-side state while developing** — serve with the semantic
tools or a `write_state` tool of your own and read results from the
`SendResult`/activity; or check `sessions/`/`runs/` directories on disk.

**Multi-agent shared context** — `Swarm`'s `sharedState` moves data between
*agents inside one server-side topology*; the swarm-level result's
`sharedContext` is returned to the caller of `swarm.run` on the server, not
to a UI. Expose an A2A agent that wraps the swarm if the UI should see its
output.

## 5. Extending what crosses the wire

Today the stream carries `step/token/thinking/tool_call/tool_result/done`.
To push custom server-side data to the UI, three supported paths:

1. Make it a **tool** — custom events come free via `tool_call`/`tool_result`.
2. Use a middleware `after_model` that embeds values in the completion text —
   they arrive as tokens.
3. For richer per-event typing, mount `AgentClient` directly (headless) and
   implement your own state mapping — the hook is intentionally thin. See
   `docs/api/react.md` and [`docs/api/typescript.md`](../api/typescript.md).