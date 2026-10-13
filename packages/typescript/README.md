# framework-agents (TypeScript)

Core of the agentic platform. See `/docs` in the monorepo root for vision, architecture, and roadmap.

## Layout

```
src/
├── index.ts            <- public API (only import from here)
├── types/              <- shared branded types, result/error envelopes
├── agent/              <- Agent loop, state, history, sessions, snapshots
├── state/              <- AgentState (KV) + ToolContext + InvocationState
├── conversation/       <- Null/SlidingWindow/Summarizing managers
├── sessions/           <- Storage backends + SessionManager + snapshots
├── swarm/              <- autonomous handoffs + shared context + safety
├── a2a/                <- A2A client/server interop
├── tools/              <- Tool interface + registry + zod validation
├── memory/             <- Memory interface + in-memory store + compaction.ts
├── models/             <- ModelProvider + OpenAI-compatible + Anthropic + Ollama + initChatModel
├── checkpoint/         <- FileCheckpointer + withFileLock (local-first durability)
├── skills/             <- SKILL.md parse/registry + discover + match
├── mcp/                <- MCP clients (HTTP/stdio) + tools bridge
├── prompts/            <- versioned prompt templates + render
├── structured/         <- responseFormat (tool/provider strategies)
├── streaming/          <- StreamEvent + collectStream
├── middleware/         <- hooks + 15 prebuilt (retry/fallback/limits/HITL/PII/summary/todos/fs/subagents…)
├── semantic/           <- GraphStore + InMemoryGraphStore (entailment in P0-sem/P1-sem)
├── orchestration/      <- sequential / parallel / router multi-agent
├── runtime/            <- run with timeout / cancellation / budgets
├── workflows/          <- DAG workflows (topo levels, retry)
├── observability/      <- event emitter + logger hooks
└── utils/              <- abort, retry, tokens helpers
```

## Scripts

```bash
pnpm build
pnpm test
pnpm lint
pnpm typecheck
```

## Serve (`framework.json` + CLI)

```bash
pnpm build
./dist/cli.js validate -c ../../framework.json
./dist/cli.js dev -c ../../framework.json --port 8484
```

```json
{
  "server": { "host": "127.0.0.1", "basePort": 8484 },
  "envFile": "./.env",
  "env": { "FRAMEWORK_ENV": "production" },
  "checkpointer": { "dir": "./checkpoints" },
  "http": { "corsOrigins": ["http://localhost:3000"] },
  "agents": [
    { "name": "helper", "model": "together:Qwen/Qwen3.5-9B", "skills": ["./skills"] },
    { "file": "./agents/local.json", "port": 8485 }
  ]
}
```

Agent entries are inline or `{ "file": "./agents/x.json" }` (+ inline overrides).
Relative paths in agent files resolve against their own dir; overrides against
`framework.json`. Env precedence: process < framework `envFile` < `env{}` <
per-agent `envFile`. Served agents get durable HITL threads (`threadId` =
context id), health (`/ok`), CORS, and per-context isolation.

## Minimal usage

```ts
import { Agent, InMemoryMemory, MockModelProvider, defineTool } from "framework-agents";
import { z } from "zod";

const getTime = defineTool({
  name: "get_time",
  description: "Returns current ISO time",
  schema: z.object({}),
  execute: async () => ({ time: new Date().toISOString() }),
});

const agent = new Agent({
  model: new MockModelProvider(),
  tools: [getTime],
  memory: new InMemoryMemory(),
});

const result = await agent.run("What time is it?");
console.log(result.text);
```
