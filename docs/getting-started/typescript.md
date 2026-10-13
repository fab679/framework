# Getting started: TypeScript

## Install

```bash
npm install framework-agents
```

Requirements: Node.js >= 20. The framework has three runtime dependencies only (dotenv, zod, zod-to-json-schema) and talks to providers with plain `fetch` — no provider SDKs.

The framework needs a model endpoint. `initChatModel` resolves a `provider:model` string against environment config:

- `together:<id>` or a bare `<id>` → TogetherAI (OpenAI-compatible)
- `openai:<id>` → OpenAI
- `anthropic:<id>` → Anthropic native Messages API
- `ollama:<id>` → local Ollama (no key)
- `custom:<id>` → any OpenAI-compatible endpoint (`baseUrl` + `apiKey` required)

## .env keys

Config loading is silent and never throws: `.env` is read from `cwd`, then up to three parent directories walk upward. Set `FRAMEWORK_ENV_PATH` to point elsewhere. When `TOGETHER_API_KEY` is set, TogetherAI wins for `loadConfig().active`; otherwise OpenAI defaults apply.

| Key | Default | Purpose |
|---|---|---|
| `TOGETHER_API_KEY` | *empty* | TogetherAI key |
| `TOGETHER_BASE_URL` | `https://api.together.xyz/v1` | TogetherAI endpoint |
| `TOGETHER_MODEL` | `Qwen/Qwen3.5-9B` | Default chat model |
| `OPENAI_API_KEY` | *empty* | OpenAI key |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI endpoint |
| `OPENAI_MODEL` | `gpt-4o-mini` | Default OpenAI model |
| `ANTHROPIC_API_KEY` | *empty* | Anthropic key (native API) |
| `ANTHROPIC_BASE_URL` | `https://api.anthropic.com` | Anthropic endpoint |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-6` | Default Anthropic model |
| `OLLAMA_BASE_URL` | `http://localhost:11434/v1` | Local Ollama endpoint |
| `OLLAMA_MODEL` | `qwen2.5` | Default Ollama model |

## First agent + first tool

`defineTool` takes `{ name, description, schema, execute }`. The zod `schema` is the contract: it is converted to a JSON Schema and sent to the provider, so the model sees the real argument shape (without `parameters`, OpenAI-compatible APIs advertise a zero-arg tool).

```ts
import { Agent, defineTool, initChatModel } from "framework-agents";
import { z } from "zod";

const getWeather = defineTool({
  name: "get_weather",
  description: "Current weather for a city",
  schema: z.object({ city: z.string() }),
  execute: async ({ city }) => ({ city, tempC: 21 }),
});

const agent = new Agent({
  name: "assistant",
  model: initChatModel("together:Qwen/Qwen3.5-9B", { stream: true }),
  tools: [getWeather],
  systemPrompt: "You are a concise weather assistant.",
  memory: undefined,                   // omit for single-shot agents
  maxSteps: 6,                         // default 6
});

const result = await agent.run("Weather in Tokyo?");
console.log(result.text, result.toolCalls, result.usage);
```

The second `execute` parameter is an optional `ToolContext` (`{ agentName, state, invocationState }`) — see [agents & tools](../guides/agents-and-tools.md).

## Streaming

`run()` drains `stream()` internally; both fire `events` hooks identically. Consume events directly with `for await`:

```ts
for await (const event of agent.stream("Weather in Tokyo?")) {
  switch (event.type) {
    case "thinking":  process.stdout.write(event.delta); break; // reasoning deltas
    case "token":     process.stdout.write(event.delta); break;
    case "tool_call": console.log("calling", event.name); break;
    case "tool_result": console.log(event.name, "→", event.output); break;
    case "done":      console.log("\nfinal:", event.text); break;
  }
}
```

Events are `step | token | thinking | tool_call | tool_result | done`. Token and thinking deltas are batched per model call; tool events yield live. Details in [streaming](../guides/streaming.md).

## Budgets

Caps are enforced after **every** model call against cumulative usage:

```ts
import { Agent, initChatModel, TokenBudgets } from "framework-agents";

const agent = new Agent({
  model: initChatModel("together:Qwen/Qwen3.5-9B"),
  budgets: {
    maxTokens: 8000,     // input + output per run (reasoning is a subset, never added)
    maxCostUsd: 0.05,    // needs a known per-model price or budgets.prices
    deadlineMs: 60_000,
  } satisfies TokenBudgets,
  maxToolCalls: 10,      // separate option; exceeded → BUDGET_EXCEEDED
});
```

Exceeding any cap throws `AgentError("BUDGET_EXCEEDED")` (non-retryable). See [budgets & pricing](../guides/budgets.md).

## Run the offline examples

The `examples/` directory in the package runs on deterministic mock providers — no API key, no cost:

```bash
cd packages/typescript
npx tsx examples/01-basic-agent.ts
npx tsx examples/02-tool-call.ts
```

Live examples (`examples/live/`) hit the real TogetherAI API and need `TOGETHER_API_KEY` — see [live examples](live-examples.md).

## Next steps

- [Agents & tools](../guides/agents-and-tools.md)
- [Middleware & HITL](../guides/middleware.md)
- [A2A & serving](../guides/a2a-serving.md)
- [React (frontend)](../guides/react.md)