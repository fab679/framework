# A2A & serving

Agent-to-Agent interop: serve any agent over HTTP (JSON-RPC + SSE), consume remote agents like local ones, wrap them as tools, and deploy from `framework.json`.

## Serving an agent: A2AServer

```ts
import { A2AServer } from "framework-agents";

const server = new A2AServer({
  card: { name: "weather", description: "Weather Q&A" },
  agentFactory: (contextId) => makeAgent(contextId),  // REQUIRED isolation
  maxContexts: 1000,          // LRU retention (default)
  port: 8484, host: "127.0.0.1",
  checkpointer,               // optional: durable HITL (threadId = contextId)
  corsOrigins: ["http://localhost:5173"],
  maxBodyBytes: 10 * 1024 * 1024,
});
const port = await server.start();  // returns the actual bound port
```

**Per-context isolation is the contract:** `agentFactory(contextId)` must return a fresh agent per contextId. Returning the SAME agent for one contextId keeps that conversation in memory across turns (fine), but never share one instance across contexts — shared history leaks conversations between users. The server caches one agent per contextId with LRU eviction past `maxContexts`. With a `checkpointer`, runs execute as `{ checkpointer, threadId: contextId }` — durable HITL per conversation. The Python `A2AServer` is identical, down to the raw SSE wire framing.

Endpoints: `GET /.well-known/agent-card.json` (the card), `GET /ok` and `/health`, `POST /` (message/send), `POST /stream` (SSE), `OPTIONS` (CORS preflight).

## Wire contract (exact)

**Request envelope — identical for `message/send` and `message/stream`:**

```json
{ "jsonrpc": "2.0", "id": "...", "method": "message/send",
  "params": { "message": { "role": "user", "parts": [{ "kind": "text", "text": "..." }] },
              "contextId": "optional" } }
```

A missing `contextId` is minted (`ctx-<timestamp36>`) and echoed **inside the result** — clients must read it there (`AgentClient.stream` surfaces it via `onContext`).

**`message/send` response:**

```json
{ "jsonrpc": "2.0", "id": "...",
  "result": { "message": { "role": "agent", "parts": [{ "kind": "text", "text": "..." }] },
              "contextId": "ctx-..." } }
```

Unknown methods return JSON-RPC error `-32601`; other pre-headers failures return `-32603` (oversized bodies answer HTTP 413).

**`message/stream` (POST /stream) — SSE frames:**

```
data: {"contextId":"ctx-...","event":{"type":"token","delta":"..."}}
data: {"contextId":"ctx-...","event":{"type":"tool_call","name":"get_weather","args":{...}}}
data: {"contextId":"ctx-...","done":true,"text":"the final answer"}
data: [DONE]
```

- Every agent `StreamEvent` is forwarded as one `{contextId, event}` frame — including the agent's own `done` event (which carries the full text). The closing `{contextId, done: true, text}` frame follows it; clients may read either.
- Failures mid-stream (wrong content type, oversized body, agent crash): `{contextId, error: "..."}` (or a bare `{error: "..."}` after a client disconnect).
- The body terminates with a `[DONE]` terminator — **it arrives both raw (`data: [DONE]`) and JSON-stringified (`data: "[DONE]"`)**: the normal end frame goes through the same stringify-everything code path as the rest, while the error path writes it raw. Consumers must tolerate both forms.
- A client disconnect aborts the agent run (the server wires the connection-close into the run's abort signal mid-stream; Python instead watches the reader in a guard task).

**Cross-language conformance:** the Python client speaks the exact TS server contract (envelope, frames, `[DONE]`) and vice versa — pinned by `packages/python/tests/test_servers_and_clients.py`.

## Clients

`AgentClient` — the frontend/server-component client (plain fetch + async iterators; usable from TanStack loaders, Next.js route handlers, plain DOM):

```ts
const client = new AgentClient("http://127.0.0.1:8484");  // or your proxy route
const { text, contextId } = await client.send("Hi");
for await (const event of client.stream("Hi", contextId, signal, { onContext: (id) => { ... } })) { ... }
await client.card();
```

```python
streamer = AgentClient("http://127.0.0.1:8484")       # stream-only client
async for event in streamer.stream("Hi", signal=None, on_context=cb):
    ...

interop = A2AClient("http://127.0.0.1:8484")          # card + message/send client
answer = await interop.send("Hi")                      # {"text": ..., "contextId": ...}
card = await interop.get_card()
```

Timeouts: `AgentClientOptions.timeout_ms` (default 300 000) governs every send/get_card/stream (TS uses `AbortSignal.timeout` per request; the Python port applies the timeout to the whole exchange so the HTTP library's own default never applies silently).

`A2AClient` — the protocol interop client: `await client.getCard()` and `await client.send(text, contextId?)`, which reads the server-assigned `contextId` from the JSON-RPC result (never made up by the caller). `send` failures map to `A2A_TRANSPORT`/`A2A_ERROR`/`A2A_CARD` errors.

## a2aAsTool

Wrap a remote agent as a local tool for orchestrators/swarms (`a2a_as_tool` in Python):

```ts
const remote = a2aAsTool(new A2AClient("http://other-host:8484"), {
  name: "research_agent",
  description: "Remote research specialist",
});
new Agent({ model, tools: [remote] });
```

The tool schema is `{ input: string, contextId?: string }`; calling it invokes the remote agent with those args and returns its text.

## handleAgentRoute (Next.js / TanStack Start bridge)

`AgentClient.stream` posts to `${endpoint}/stream` — so mount `handleAgentRoute` at **`app/api/agent/stream/route.ts`** and point `useAgent({ endpoint: "/api/agent" })` (or an `AgentClient`) at the parent:

```ts
// app/api/agent/stream/route.ts
import { AgentClient, handleAgentRoute } from "framework-agents";

const client = new AgentClient(process.env.AGENT_URL!);
export const POST = (req: Request) => handleAgentRoute(req, client);
```

`handleAgentRoute(req, client)` accepts `POST { text, contextId? }` and returns an SSE `Response`: `{ contextId, event }` frames, a closing `{ contextId }` frame, then `[DONE]`; errors become `{ error }` frames. The browser disconnect (`req.signal`) stops the upstream agent run — the browser and the server-side run never diverge. The same function works in a TanStack Start server route. See [React](react.md).

## framework.json + the serve CLI

`framework.json` declares agents; relative paths resolve against the config file's own directory (a `{ "file": ... }` agent entry resolves pathy keys against the agent file's dir; inline overrides resolve against framework.json — per key):

```json
{
  "server": { "host": "127.0.0.1", "basePort": 8484 },
  "envFile": [".env"],
  "env": { "TOGETHER_API_KEY": "" },
  "checkpointer": { "dir": ".data" },
  "http": { "corsOrigins": [] },
  "agents": [
    { "name": "helper", "model": "together:Qwen/Qwen3.5-9B",
      "systemPrompt": "You are a helpful agent. Use tools when needed. Be concise.",
      "maxSteps": 8, "skills": ["./skills"], "filesystem": "./workspace",
      "todos": true, "sessionDir": ".data/sessions", "port": 9000, "envFile": "./helper.env" }
  ]
}
```

`loadFrameworkConfig` / `load_framework_config` applies framework-level `envFile`s (gap-filling; real environment wins), then the inline `env` mapping (overrides). Each agent's `model` string goes through **`initChatModel` / `init_chat_model`** — provider prefixes (`together:`, `openai:`, `anthropic:`, `ollama:`, `custom:` with `baseUrl`/`apiKey`) work exactly as in [getting started](../getting-started/typescript.md). Skills append an "Available skill packs (mention $name to invoke)" catalog to the system prompt; `filesystem` and `todos` wire their [middleware](middleware.md); `sessionDir` gets a `SessionManager` scoped `{name}:{contextId}`.

`serveFramework` / `serve_framework` builds a fresh agent **per A2A contextId**, shares one `FileCheckpointer` when `checkpointer.dir` is set, and serves each definition on its own port (`port` pins one; otherwise the ports increment from `basePort`). CORS (`http.corsOrigins`) is reflected as `Access-Control-Allow-Origin` plus `OPTIONS` handling.

CLI — `framework-serve` (npm bin) in TS, `python -m framework_agents.serve` in Python. Same commands and flags in both:

```bash
framework-serve dev [-c framework.json] [--host H] [--port N]   # local dev server (default)
framework-serve serve|up [-c framework.json]                   # serve agents
framework-serve validate [-c framework.json]                  # validate config, print agents
```

Python deviations (pinned in `serve.py`): agents are built **lazily** inside the contextId-scoped factory (a missing API key can't take the whole port ladder down), and env files load with setdefault semantics — the process environment always wins.