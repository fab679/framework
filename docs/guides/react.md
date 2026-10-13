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

- `token` → the delta appends to the last `turns` entry (the assistant turn created at send time).
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