# framework-agents-react

React hooks for [`framework-agents`](https://www.npmjs.com/package/framework-agents): turn an agent server's SSE stream into chat state with one hook — live tokens and thinking, tool activity, automatic multi-turn contexts, and awaited results.

## Install

```bash
npm install framework-agents-react framework-agents
```

(react `>=18` peer. Works with Next.js / TanStack Start: mount the repo's `handleAgentRoute` at `app/api/agent/stream/route.ts` and point the hook's `endpoint` at `/api/agent`.)

## Use

```tsx
import { useAgent } from "framework-agents-react";

function Chat() {
  const { turns, thinking, activity, status, error, contextId, send, cancel, reset } =
    useAgent({ endpoint: "/api/agent" });

  return (
    <>
      {turns.map((t, i) => <p key={i}><b>{t.role}:</b> {t.text}</p>)}
      {thinking && <pre className="thinking">{thinking}</pre>}
      <button onClick={() => send("Explain black holes")} disabled={status === "streaming"}>
        Ask
      </button>
    </>
  );
}
```

## What the hook gives you

- `turns` — user/assistant messages; token deltas append live; a final `done` event reconciles the answer even from providers that only return full text in the terminal frame.
- `thinking` — reasoning deltas, wiped on each send.
- `activity` — feed lines (`calling get_weather…`, `get_weather done`) for a live trace pane.
- `status` — `"idle" | "streaming" | "error"` with last-send-wins semantics (a second send never lets the first clobber it), and errors surfaced in `error`.
- `contextId` — the server-assigned conversation handle, automatically reused by the next `send()` (multi-turn memory without effort); `reset()` clears it.
- **`await send(text)` resolves with `{ text, toolCalls, contextId }`** — so submit handlers can await the final answer, react to the tools used, and store the conversation handle wherever they like.
- Lifecycle guarantees: abort on unmount (the server-side run stops when the client stops), empty assistant turns dropped on abort/error, and no race between overlapping sends.