# React (frontend)

`framework-agents-react` ships one hook, `useAgent` (source:
`packages/react/src/useAgent.ts`, 154 lines — read it; everything below is
generated from that file). It turns a Framework agent server (or a proxy
route) into live component state, and is a thin layer over `AgentClient`
from `framework-agents`.

```bash
npm install framework-agents-react   # peerDeps: react >= 18, framework-agents
```

## Options

```ts
useAgent({ endpoint, contextId, fetchFn });
```

- `endpoint: string` — agent server URL or proxy-route parent. POSTs go to
  `${endpoint}/stream`, so a Next.js/TanStack proxy mounts at
  `/api/agent/stream` and the hook uses `"/api/agent"` (see
  [A2A & serving](a2a-serving.md)).
- `contextId?: string` — persisted conversation handle; omit to let the
  server assign one. The hook remembers the server-assigned id across sends.
- `fetchFn?: typeof fetch` — injectable fetch (tests, adapters).

## Returned state and actions

```ts
const { turns, thinking, activity, status, error, contextId, send, cancel, reset } =
  useAgent({ endpoint: "/api/agent" });
```

| Field | Type | Meaning |
|---|---|---|
| `turns` | `ChatTurn[]` (`{ role: "user" \| "assistant", text }`) | full transcript, streaming into the last assistant turn |
| `thinking` | `string` | accumulated reasoning deltas (wiped on each send) |
| `activity` | `string[]` | tool activity lines (`calling x…`, `x done`) |
| `status` | `"idle" \| "streaming" \| "error"` | run status |
| `error` | `string \| null` | last error message |
| `contextId` | `string \| undefined` | the context handle this hook instance is using (your prop or server-assigned) |
| `send` | `(text: string) => Promise<SendResult>` | send a message and await the outcome — resolves `{ text, toolCalls, contextId }` (`toolCalls` are names in call order) |
| `cancel` | `() => void` | abort the in-flight stream (status `idle`) |
| `reset` | `() => void` | abort, clear turns/thinking/activity/error, and drop the context id (fresh conversation) |

`SendResult` (exported from `framework-agents-react`):

```ts
interface SendResult {
  text: string;       // final answer text of THIS send
  toolCalls: string[]; // tool names in call order
  contextId?: string;  // handle used for this send (source of truth for the next one)
}
```

## Event mapping (hook internals)

- `token` → the delta appends to the last `turns` entry (the assistant turn created at send time); token deltas arrive **live during streaming**, not batched after the model call completes.
- `thinking` → appends to `thinking`.
- `done` (when it carries text) → **reconcile**: some providers/middleware stream no deltas and deliver the full answer only in the done event — the last turn is replaced with the final text instead of staying empty. `finalText` is also what `send` resolves with.
- `tool_call` → appends `` `calling ${event.name}…` `` to `activity` and pushes the name into the send's `toolCalls`.
- `tool_result` → appends `` `${event.name} done` `` to `activity`.
- `step` events are ignored by the hook.

An error frame from the server throws out of the `for await` loop; the hook
catches it (see lifecycle below) and reported through `error`/`status`.

## Full implementation — a complete chat component

Copy this file verbatim for a production-ready chat surface: transcript,
thinking pane, tool activity, form with disabled state while streaming,
stop/reset, persisted conversation handle, and awaited `SendResult` handling.

```tsx
// components/AgentChat.tsx
"use client";

import { useEffect, useState } from "react";
import { useAgent, type ChatTurn } from "framework-agents-react";

const STORAGE_KEY = "framework:contextId"; // one conversation per browser; scope per user for multi-user

export function AgentChat({ endpoint = "/api/agent" }: { endpoint?: string }) {
  const [draft, setDraft] = useState("");
  const [savedContext, setSavedContext] = useState<string | undefined>(
    typeof window === "undefined" ? undefined : localStorage.getItem(STORAGE_KEY) ?? undefined,
  );

  const {
    turns, thinking, activity, status, error, contextId,
    send, cancel, reset,
  } = useAgent({ endpoint, contextId: savedContext });

  // Persist the server-assigned handle: reloads/reconnects continue the thread.
  useEffect(() => {
    if (contextId) localStorage.setItem(STORAGE_KEY, contextId);
  }, [contextId]);

  const startConversation = () => reset(); // drops everything incl. the handle

  async function onSubmit() {
    const text = draft.trim();
    if (!text) return;
    setDraft("");
    const result = await send(text);
    // result: { text, toolCalls, contextId } — e.g. log the tools the agent used.
    if (result.toolCalls.length) console.log("tools used:", result.toolCalls);
  }

  return (
    <div style={{ maxWidth: 720 }}>
      <ol>
        {turns.map((t, i) => (
          <li key={i} style={{ listStyle: "none" }}>
            <b>{t.role === "user" ? "You" : "Agent"}:</b> {t.text}
            {i === turns.length - 1 && t.role === "assistant" && status === "streaming" && "▌"}
          </li>
        ))}
      </ol>

      {thinking && (
        <details style={{ opacity: 0.7 }}>
          <summary>thinking…</summary>
          <pre>{thinking}</pre>
        </details>
      )}

      {activity.length > 0 && (
        <ul style={{ opacity: 0.7 }}>
          {activity.map((a, i) => (
            <li key={i} style={{ listStyle: "none" }}>{a}</li>
          ))}
        </ul>
      )}

      {contextId && <small>conversation: {contextId}</small>}

      <form
        onSubmit={(e) => { e.preventDefault(); void onSubmit(); }}
        style={{ display: "flex", gap: 8, marginTop: 12 }}
      >
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder='Ask anything…'
          disabled={status === "streaming"}
          style={{ flexGrow: 1 }}
        />
        {status === "streaming" ? (
          <button type="button" onClick={cancel}>Stop</button>
        ) : (
          <button type="submit" disabled={!draft.trim()}>Send</button>
        )}
      </form>

      {status === "error" && (
        <p style={{ color: "crimson" }}>{error}</p>
      )}
      <button onClick={startConversation} style={{ marginTop: 8 }}>New conversation</button>
    </div>
  );
}
```

And the awaiting-`SendResult` pattern in a submit handler (async flows,
tool reactions, conversation bookkeeping):

```tsx
async function onSubmit() {
  const { text, toolCalls, contextId } = await send("Summarize this PR and file it");
  if (toolCalls.includes("file_pr")) refetchQueue();          // react to the tools used
  analytics.track("agent_reply", { toolCalls, contextId });
  // text is the FINAL answer even if the provider streamed no deltas:
  console.log(text);
}
```

## Lifecycle guarantees

- **Abort on unmount:** a cleanup effect aborts the in-flight `AbortController` on unmount — otherwise the SSE connection (and the server-side agent run) would keep going by themselves.
- **Newer sends win:** `send` aborts any previous send first and every stage compares `abortRef.current === controller`. If a newer `send` took over (or this one was aborted), the older send stops touching state and returns `{ text, toolCalls, contextId }` without setting `status`/`error`.
- **No empty assistant bubbles:** aborted or erroring sends drop the trailing empty assistant turn they created at send time.
- **Context continuity:** `onContext` stores the server-assigned `contextId` in a ref — the next `send` continues the same conversation automatically; you never need to thread it manually.
- `cancel`/`reset` abort and set `status: "idle"`; the streaming fetch's `finally` releases the connection and stops the upstream agent (server-side disconnect handling cancels the run).

## Server side: the proxy route

Server side, mount `handleAgentRoute` at `app/api/agent/stream/route.ts` and
point the hook at `/api/agent`:

```ts
import { AgentClient, handleAgentRoute } from "framework-agents";

const client = new AgentClient(process.env.AGENT_URL!);
export const POST = (req: Request) => handleAgentRoute(req, client);
```

`AGENT_URL` points at any Framework agent server — a `framework.json`-served
stack (`serveFramework`), your own `A2AServer`, or the direct provider-backed
setup. The proxy contract is `POST { text, contextId? }` → SSE frames.

## Next.js implementation example (full wiring)

A complete two-file Next.js (App Router) setup — one route handler proxying
the agent, one page rendering the chat — plus env config.

### 1. `app/api/agent/stream/route.ts` — the proxy route

`handleAgentRoute` reads `POST { text, contextId? }`, forwards it upstream,
and converts the run into SSE frames — including the disconnect propagation
(a closed browser tab stops the upstream agent run).

```ts
import { AgentClient, handleAgentRoute } from "framework-agents";

// process.env.AGENT_URL points at any Framework agent server:
//  - a framework.json stack:      http://127.0.0.1:8484        (serveFramework)
//  - your own A2AServer:           http://127.0.0.1:9100
// The URL comes from the server's agent card / /ok endpoint to verify.
const upstream = new AgentClient(process.env.AGENT_URL!, { timeoutMs: 300_000 });

export const runtime = "nodejs";        // the proxy needs Node (not edge)

export const POST = (req: Request) => handleAgentRoute(req, upstream);
export const maxDuration = 300;         // keep long agent runs alive
```

Also stream the agent card for discovery/debugging if you like:

```ts
export async function GET() {
  const card = await upstream.getCard();
  return Response.json(card);
}
```

### 2. `app/page.tsx` — the chat UI

```tsx
"use client";

import { AgentChat } from "@/components/AgentChat";

export default function Home() {
  return <AgentChat endpoint="/api/agent" />;
}
```

(`AgentChat` is the complete component above — drop it into
`components/AgentChat.tsx`.)

### 3. `.env.local`

```bash
# Server-side: where the Framework agent server lives.
AGENT_URL=http://127.0.0.1:8484
```

### 4. Multi-turn — what happens end-to-end

1. First send posts `{ text }` with no `contextId` → the server mints
   `ctx-…`, and every SSE frame carries it → `send` resolves (or the hook's
   `contextId` state updates) → the component persists it (localStorage).
2. Reload/navigation: the component re-initializes with the stored handle;
   the served agent (its per-context memory/session) continues the thread.
3. Second send posts `{ text, contextId }` → the same agent instance
   (`agentFactory(contextId)`, LRU-cached) answers with prior history.
4. The user hits Stop → the browser aborts → `req.signal` fires → the proxy
   stops pulling frames → the upstream `A2AServer`/serve run is cancelled.

### 5. Deploying the agent yourself instead of proxying

Alternatively run the agent server *in* the Next.js process
(`serverExternalPackages` for any native deps) — or skip the proxy entirely
for trusted intranet clients and point `useAgent` at
`http://127.0.0.1:8484` directly; the hook is transport-agnostic. The proxy
exists so the browser never talks to the agent network directly and so
`AGENT_URL` secrets stay server-side.


## Persisting conversations across page loads

`contextId` round-trips through the server: pass it back and the served agent
(its per-context memory/session) continues the thread. The full state model —
what lives in the UI vs. what lives on the server per context — is in
[Sessions, threads & state](sessions-and-state.md).

## TanStack Start notes

- The hook works with TanStack Start exactly like Next.js: `endpoint` is just
  a URL serving `AgentClient`'s `POST {endpoint}/stream` SSE contract — mount
  the same `handleAgentRoute` bridge as a server route.
- For data-fetching integration, drive `send` from a TanStack Query mutation
  and read `turns`/`status` for UI state.
- Headless alternative: use `AgentClient` directly inside `useMutation` when
  you only need final answers:

```ts
import { useMutation } from "@tanstack/react-query";
import { AgentClient } from "framework-agents";

const client = new AgentClient("http://127.0.0.1:8484");
const answer = useMutation({
  mutationFn: (text: string) => client.send(text).then((r) => r.text),
});
```

## Related

- [Sessions, threads & state](sessions-and-state.md) — the full state ownership model
- [A2A & serving](a2a-serving.md) — the wire contract and server setup
- [Streaming & events](streaming.md) — the event types mapped above