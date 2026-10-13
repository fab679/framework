# React hook (`framework-agents-react`)

`framework-agents-react` is a thin React layer over [`AgentClient`](typescript.md#client-module) from `framework-agents`. It turns the SSE stream that the Framework A2A server (or a `handleAgentRoute` proxy route) emits into React state: chat turns, streaming text, thinking deltas, tool activity, and the server-assigned conversation handle.

The hook speaks the `POST {endpoint}/stream` SSE contract of `AgentClient.stream` — it works against the Framework serve stack, a Next.js/TanStack Start proxy built with `handleAgentRoute`, or anything else that speaks the same wire format.

## Installation

```bash
npm install framework-agents framework-agents-react
```

Peer dependency: `react >= 18`.

Both packages in one command matters: `framework-agents-react` imports `AgentClient` from `framework-agents`, so the app and the hook package must resolve to a **single** `framework-agents` instance. If your lockfile ends up with two copies (e.g. because of a version range mismatch or a transitive dependency pinning an older one), you get two `AgentClient` classes, two `StreamEvent` type identities, and two distinct `AgentError` classes — `instanceof` checks and type inference stop lining up. Keep them in lockstep (or use npm/pnpm overrides) so exactly one copy is installed.

## Quick start

```tsx
import { useAgent } from "framework-agents-react";

function Chat() {
  const { turns, thinking, activity, status, error, contextId, send, cancel, reset } =
    useAgent({ endpoint: "/api/agent" });

  // send is async — drive it from an event handler / query mutation
  async function onSubmit() {
    const result = await send("Explain black holes");
  }
}
```

`contextId` is the server-assigned conversation handle. Pass it back via `contextId` in options to continue the same conversation — or just reuse this hook instance: `send` keeps the context automatically. To persist a conversation across page loads, store the returned `contextId` (or `SendResult.contextId`) and feed it into `UseAgentOptions.contextId` on the next mount.

## `useAgent`

```ts
import { useAgent } from "framework-agents-react";

function useAgent(options: UseAgentOptions): {
  turns: ChatTurn[];
  thinking: string;
  activity: string[];
  status: AgentStatus;
  error: string | null;
  contextId: string | undefined;
  send: (text: string) => Promise<SendResult>;
  cancel: () => void;
  reset: () => void;
};
```

### `UseAgentOptions`

```ts
export interface UseAgentOptions {
  /** Agent server URL or proxy route parent (POSTs go to `${endpoint}/stream`). */
  endpoint: string;
  /** Persisted conversation handle; omit to let the server assign one. */
  contextId?: string;
  fetchFn?: typeof fetch;
}
```

| Field | Type | Notes |
|---|---|---|
| `endpoint` | `string` | Required. Parent URL of the agent server or proxy route — the hook POSTs to `${endpoint}/stream` (via `AgentClient`). For a Next.js route handler mounted with `handleAgentRoute` at `app/api/agent/stream/route.ts`, pass `"/api/agent"`. Trailing slashes in `contextId` are stripped by `AgentClient`; changing `endpoint` (or `fetchFn`) re-creates the `send` callback. |
| `contextId` | `string` (optional) | Persisted conversation handle for continuing an existing conversation. Omit to let the server assign one; the hook captures whatever the server assigns and reuses it on subsequent `send` calls. |
| `fetchFn` | `typeof fetch` (optional) | Custom fetch implementation (proxied fetch, instrumented fetch, test doubles). Forwarded to `AgentClient`, which defaults to global `fetch`. |

### `ChatTurn`

```ts
export interface ChatTurn {
  role: "user" | "assistant";
  text: string;
}
```

One bubbles-in-the-transcript entry. Each `send` optimistically appends the user turn plus an empty assistant turn; `token` deltas fill the assistant turn.

### `AgentStatus`

```ts
export type AgentStatus = "idle" | "streaming" | "error";
```

- `"idle"` — no active send (initial state, after a completed send, after `cancel`, after `reset`).
- `"streaming"` — a send is in flight.
- `"error"` — the last send failed; the message is in `error`.

### `SendResult`

```ts
/** What a completed send resolves with — toolCalls are names in call order. */
export interface SendResult {
  text: string;
  toolCalls: string[];
  /** The conversation handle this send used (server-assigned or your prop). */
  contextId?: string;
}
```

| Field | Type | Notes |
|---|---|---|
| `text` | `string` | Final assistant text. Empty until `done` arrives for providers/middleware that don't stream deltas; for aborted sends, whatever streamed before the abort. |
| `toolCalls` | `string[]` | Tool names in call order — only names, no args or outputs. |
| `contextId` | `string` (optional) | The conversation handle this send used — either your `options.contextId` prop or the server-assigned one. |

## Returned state and actions

| Field | Type | Behavior |
|---|---|---|
| `turns` | `ChatTurn[]` | The transcript. Streaming `token` deltas mutate the last assistant turn. `done` with text reconciles the last assistant turn with the full answer (for providers that don't stream deltas). |
| `thinking` | `string` | Accumulated reasoning/thinking deltas (`thinking` events). Reset to `""` at the start of each send and by `reset`. |
| `activity` | `string[]` | Human-readable tool activity log: `"calling {name}…"` pushed on each `tool_call`, `"{name} done"` on each `tool_result`. Cleared only by `reset` (sends append, so history accumulates within the hook's lifetime). |
| `status` | `AgentStatus` | Lifecycle of the latest send. |
| `error` | `string` \| `null` | Error message string (`err instanceof Error ? err.message : String(err)`) when status is `"error"`; `null` otherwise. Cleared at the start of each `send` and by `reset`. |
| `contextId` | `string` \| `undefined` | The current conversation handle: your `options.contextId` initially, replaced with the server-assigned id as soon as the SSE envelope carries one (`onContext`). Treat it as read state; the hook reuses it internally for the next `send`. |
| `send` | `(text: string) => Promise<SendResult>` | Sends a message. Aborts any prior in-flight send first (see [guarantees](#guarantees)), appends the user turn plus an empty assistant turn, sets `"streaming"`, consumes the SSE stream, and resolves with the result even when aborted mid-flight (superseded/aborted sends resolve with partial `text`, no rejection). |
| `cancel` | `() => void` | Aborts the in-flight send (if any) and sets status back to `"idle"`. |
| `reset` | `() => void` | Aborts any in-flight send and clears everything: `turns`, `thinking`, `activity`, `error`, status to `"idle"`, and drops the conversation handle — the **next `send` starts a fresh conversation** (new server-assigned `contextId`). |

## Event mapping

The hook consumes `AgentClient.stream`, which yields the server's `StreamEvent` values (and reports the envelope's `contextId` via `onContext`). Mapping:

| Stream / SSE event | Hook surface |
|---|---|
| `token` (`delta`) | Appends `delta` to the last assistant turn in `turns`. |
| `thinking` (`delta`) | Appends `delta` to the `thinking` string. |
| `done` (`text`, only if non-empty) | Overwrites the last assistant turn with the full final text — reconciles providers/middleware that report the entire answer only at the end. Also becomes `SendResult.text`. |
| `tool_call` (`name`, `args`) | Pushes `name` into the send's `toolCalls` and appends `"calling {name}…"` to `activity`. Args are not surfaced. |
| `tool_result` (`name`, `output`) | Appends `"{name} done"` to `activity`. Output is not surfaced. |
| `step` (`step`) | Not surfaced — ignored by the hook (step counting is internal to the agent loop). |
| SSE envelope `contextId` | Updates the hook's `contextId` state and is reused for subsequent sends. |
| SSE envelope `error` | Thrown by `AgentClient` as an `AgentError` (`A2A_ERROR`, not retryable); caught by `send`, which then sets `error` + status `"error"` and drops the empty assistant turn. |
| SSE transport failure (network/HTTP) | Thrown by `AgentClient` as an `AgentError` (`A2A_TRANSPORT`); surface path same as above. |

## Guarantees

- **Unmount aborts the stream.** A `useEffect` cleanup aborts the in-flight `AbortController` on unmount; `AgentClient.stream` cancels the SSE connection on early exit, which stops the server-side agent run instead of letting it finish in the background (with `handleAgentRoute`, the browser disconnect propagates via `req.signal`).
- **Last-send-wins.** `send` aborts the previous send's controller before starting a new one. A superseded (or user-aborted) send never touches `error`/`status` — only the send that still owns `abortRef` (and was not aborted) sets the final `"idle"` status; a superseded send's throw is swallowed and resolves with its partial result. Likewise, if a newer `send()` took over, the older one lets it own the status.
- **Empty-turn cleanup.** Aborted/erroring sends must not leave an empty assistant bubble: when the abort is observed in the stream loop, or when the send throws a genuine error, the trailing empty assistant turn created optimistically is removed. Superseded sends do not mutate the shared `turns` state at all — the newer send takes over the transcript.
- **`cancel` vs `reset`.** `cancel` stops the stream but keeps turns/handle; `reset` stops the stream, wipes state, and forgets the conversation handle, so the next `send` opens a new conversation.
- **Errors never reject.** `send` always resolves with a `SendResult`, even on failure — check `status`/`error` for the failure mode instead of wrapping in try/catch.

## Serving the backend

The hook needs an endpoint that serves `POST {endpoint}/stream` with SSE. Options:

- **Framework serve stack** — point `endpoint` at the Framework A2A server base URL (or an HTTPS proxy in front of it).
- **Next.js / TanStack Start proxy route** — mount `handleAgentRoute` from `framework-agents`:

```ts
// app/api/agent/stream/route.ts
import { AgentClient, handleAgentRoute } from "framework-agents";

const client = new AgentClient(process.env.AGENT_URL!);
export const POST = (req: Request) => handleAgentRoute(req, client);
```

Then `useAgent({ endpoint: "/api/agent" })` streams through the proxy; the server-side upstream run stops when the browser disconnects. For data-fetching integration, drive `send` from a TanStack Query mutation and read `turns`/`status` for UI state.