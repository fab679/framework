# React (frontend)

`framework-agents-react` ships one hook, `useAgent` (in `packages/react/src/useAgent.ts`), which turns a Framework agent server (or a proxy route) into live component state. It is a thin layer over `AgentClient` from `framework-agents` — the same client works headless.

```bash
npm install framework-agents-react   # peerDeps: react >= 18
```

## Options

```ts
useAgent({ endpoint, contextId, fetchFn });
```

- `endpoint: string` — agent server URL or proxy-route parent. POSTs go to `${endpoint}/stream`, so a Next.js/TanStack proxy mounts at `/api/agent/stream` and the hook uses `"/api/agent"` (see [A2A & serving](a2a-serving.md)).
- `contextId?: string` — persisted conversation handle; omit to let the server assign one. The hook remembers the server-assigned id across sends.
- `fetchFn?: typeof fetch` — injectable fetch (tests, adapters).

## Returned state and actions

```ts
const { turns, thinking, activity, status, error, contextId, send, cancel, reset } =
  useAgent({ endpoint: "/api/agent" });
```

| Field | Type | Meaning |
|---|---|---|
| `turns` | `ChatTurn[]` (`{ role: "user" \| "assistant", text }`) | full transcript, streaming into the last assistant turn |
| `thinking` | `string` | accumulated reasoning deltas |
| `activity` | `string[]` | tool activity lines (`calling x…`, `x done`) |
| `status` | `"idle" \| "streaming" \| "error"` | run status |
| `error` | `string \| null` | last error message |
| `contextId` | `string \| undefined` | the context handle this hook instance is using (your prop or server-assigned) |
| `send` | `(text: string) => Promise<SendResult>` | send a message; returns `{ text, toolCalls, contextId }` (`toolCalls` are names in call order) |
| `cancel` | `() => void` | abort the in-flight stream, status back to `idle` |
| `reset` | `() => void` | abort, clear turns/thinking/activity/error, and drop the context id (fresh conversation) |

## Event mapping

- `token` → the delta appends to the last `turns` entry (the assistant turn created at send time).
- `thinking` → appends to `thinking`.
- `done` (when it carries text) → **reconcile**: some providers/middleware stream no deltas and deliver the full answer only in the done event — the last turn is replaced with the final text instead of staying empty.
- `tool_call` → appends `` `calling ${event.name}…` `` to `activity` and pushes the name into the send's `toolCalls`.
- `tool_result` → appends `` `${event.name} done` `` to `activity`.
- `step` events are ignored by the hook.

An error frame from the server throws out of the `for await` loop; the hook catches it (see lifecycle below).

## Lifecycle guarantees

- **Abort on unmount:** a cleanup effect aborts the in-flight `AbortController` on unmount — otherwise the SSE connection (and the server-side agent run) would keep going by themselves.
- **Newer sends win:** `send` aborts any previous send first and every stage compares `abortRef.current === controller`. If a newer `send` took over (or this one was aborted), the older send stops touching state and returns `{ text, toolCalls, contextId }` without setting `status`/`error`.
- **No empty assistant bubbles:** aborted or erroring sends drop the trailing empty assistant turn they created at send time.
- **Context continuity:** `onContext` stores the server-assigned `contextId` in a ref — the next `send` continues the same conversation automatically; you never need to thread it manually.
- `cancel`/`reset` abort and set `status: "idle"`; the streaming fetch's `finally` releases the connection and stops the upstream agent.

## Complete minimal component

```tsx
import { useAgent } from "framework-agents-react";

function Chat() {
  const { turns, thinking, activity, status, error, send, cancel, reset } =
    useAgent({ endpoint: "/api/agent" }); // or "http://127.0.0.1:8484" direct

  return (
    <div>
      {turns.map((t, i) => (
        <p key={i}><b>{t.role}:</b> {t.text}</p>
      ))}
      {thinking && <p style={{ opacity: 0.6 }}>{thinking}</p>}
      {activity.map((a, i) => <small key={i}>{a}</small>)}
      {status === "streaming" && <button onClick={cancel}>Stop</button>}
      {error && <p>{error} <button onClick={reset}>Reset</button></p>}
      <button onClick={() => void send("Hello")}>Send</button>
    </div>
  );
}
```

Server side, mount `handleAgentRoute` at `app/api/agent/stream/route.ts` and point the hook at `/api/agent`:

```ts
import { AgentClient, handleAgentRoute } from "framework-agents";

const client = new AgentClient(process.env.AGENT_URL!);
export const POST = (req: Request) => handleAgentRoute(req, client);
```

## TanStack Start notes (from the hook's own docstring)

- The hook works with TanStack Start the same way: `endpoint` is just a URL that serves `AgentClient`'s `POST {endpoint}/stream` SSE contract — mount the same `handleAgentRoute` bridge as a server route.
- For data-fetching integration, drive `send` from a TanStack Query mutation and read `turns`/`status` for UI state.
- Headless alternative: use `AgentClient` directly inside `useMutation` when you only need final answers:

```ts
import { useMutation } from "@tanstack/react-query";
import { AgentClient } from "framework-agents";

const client = new AgentClient("http://127.0.0.1:8484");
const answer = useMutation({
  mutationFn: (text: string) => client.send(text).then((r) => r.text),
});
```

## Related

- [A2A & serving](a2a-serving.md) — the wire contract and server setup
- [Streaming & events](streaming.md) — the event types mapped above