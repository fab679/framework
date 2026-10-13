# framework-agents

TypeScript agent framework: a ReAct loop with validated tools, streaming, token/cost budgets, a middleware stack with human-in-the-loop, multi-agent orchestration (sequential / parallel / router, DAG workflows, swarms), a typed semantic graph, A2A serving, MCP clients, and a React companion package.

- **Runtime**: Node >= 20. Two production dependencies only (TogetherAI/OpenAI-compatible, Anthropic, and Ollama providers are `fetch`-based — no SDK lock-in).
- **Docs**: full guides + API reference live in the repo (`docs/` is an MkDocs site; published via GitHub Pages).
- **Live-tested**: every feature has an offline + gated live test suite hitting the real provider APIs.

## Install

```bash
npm install framework-agents
```

Needs an OpenAI-compatible endpoint, Anthropic, or a local Ollama — configured via env keys (`TOGETHER_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `OLLAMA_*`) or passed inline.

## Agent + tool in 15 lines

```ts
import { Agent, defineTool, initChatModel } from "framework-agents";
import { z } from "zod";

const weather = defineTool({
  name: "get_weather",
  description: "Get the weather for a city.",
  schema: z.object({ city: z.string() }),           // reaches the provider wire: models see real arg shapes
  execute: async ({ city }) => `18c in ${city}`,
});

const agent = new Agent({
  model: initChatModel("together:Qwen/Qwen3.5-9B", { maxTokens: 1500 }),
  tools: [weather],
  budgets: { maxTokens: 5000, maxCostUsd: 0.05 },   // enforced after every model call
});

const res = await agent.run("What's the weather in Tokyo?");
console.log(res.text, res.toolCalls, res.usage.costUsd);
```

## Streaming

```ts
for await (const e of agent.stream("Plan my trip to Berlin")) {
  if (e.type === "token") process.stdout.write(e.delta);
  if (e.type === "tool_call") console.log("\n[tool]", e.name);
}
```

Token deltas arrive per model call; tool events stream live the moment the model dispatches (and the moment the result lands). Completions carry real `usage` (with `reasoningTokens`) and `costUsd` computed from the built-in price table — never guessed.

## Surface

| Area | Highlights |
|---|---|
| Event streaming | `run()` / `stream()` with typed events, abort + timeout, both `reasoning` field families (Together/OpenAI) parsed |
| Structured output | zod schemas as native `response_format` or a self-repairing tool loop |
| Budgets | token / USD / per-run deadline enforcement, per-model pricing table |
| Middleware | before/after model+tool hooks, retries with backoff honoring retryable verdicts, human-in-the-loop (checkpointed resume, wildcard approvals), fallback chains, PII redaction, filesystem + subagents + todos |
| State | persist history/state across restarts, snapshots, `pause()`/`resume()`, message edit + branching with timelines |
| Multi-agent | orchestration (3 strategies), DAG workflows per-node retries, autonomous handoff swarms with loop detection |
| Semantic | graph store with RDFS/OWL entailment + `why()` proofs, SHACL shapes, a validated write path, entity linking, ontology migration with conformance batteries |
| Serving | A2A server/client (JSON-RPC + SSE), per-context isolation, MCP stdio + HTTP clients |
| Python | same surface in `framework_agents` on PyPI — the A2A wire contract is conformance-tested across both stacks |

## Publishing docs

`docs/` builds with MkDocs (`mkdocs build --strict`) and deploys to GitHub Pages via `.github/workflows/docs.yml`.