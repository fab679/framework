# framework-agents-react

React hooks for Framework agents. Thin layer over `AgentClient` (`framework-agents`): the same client works headless in TanStack Query loaders/mutations, Next.js route handlers, or plain DOM code.

## useAgent

```tsx
import { useAgent } from "framework-agents-react";

function Chat() {
  const { turns, thinking, activity, status, error, send, cancel, reset } =
    useAgent({ endpoint: "/api/agent" }); // or "http://127.0.0.1:8484" direct

  return (
    <div>
      {turns.map((t, i) => <p key={i}><b>{t.role}:</b> {t.text}</p>)}
      {thinking && <p style={{ opacity: 0.6 }}>{thinking}</p>}
      {activity.map((a, i) => <small key={i}>{a}</small>)}
      {status === "streaming" && <button onClick={cancel}>Stop</button>}
      {error && <p>{error} <button onClick={reset}>Reset</button></p>}
      <button onClick={() => void send("Hello")}>Send</button>
    </div>
  );
}
```

## Next.js proxy route (`app/api/agent/route.ts`)

```ts
import { AgentClient, handleAgentRoute } from "framework-agents";

const client = new AgentClient(process.env.AGENT_URL!);
export const POST = (req: Request) => handleAgentRoute(req, client);
```

## TanStack Query

```ts
import { useMutation } from "@tanstack/react-query";
import { AgentClient } from "framework-agents";

const client = new AgentClient("http://127.0.0.1:8484");
const answer = useMutation({
  mutationFn: (text: string) => client.send(text).then((r) => r.text),
});
```
