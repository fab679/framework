# Framework Agents

A TypeScript-first agent platform with a Python parity port. One set of primitives in both languages: a ReAct agent loop, schema-validated tools, event streaming, token/cost budgets, composable middleware, multi-agent orchestration, a semantic graph layer, A2A serving, MCP clients, and React bindings.

| Package | Registry | Language |
|---|---|---|
| `framework-agents` | npm | TypeScript (reference implementation) |
| `framework-agents-react` | npm | TypeScript (React hooks) |
| `framework-agents` | PyPI | Python port (imports as `framework_agents`) |

## Quickstart (TypeScript)

```ts
import { Agent, defineTool, initChatModel, InMemoryMemory, consoleEvents } from "framework-agents";
import { z } from "zod";

const getTime = defineTool({
  name: "get_time",
  description: "Returns the current time in ISO format",
  schema: z.object({}), // zod schema = the validated argument shape
  execute: async () => ({ time: new Date().toISOString() }),
});

const agent = new Agent({
  name: "demo",
  model: initChatModel("together:Qwen/Qwen3.5-9B"), // reads TOGETHER_API_KEY from .env
  tools: [getTime],
  memory: new InMemoryMemory(),
  events: consoleEvents(),
});

// Fire-and-forget result, or stream events as they happen:
const result = await agent.run("What time is it?");
console.log(result.text, result.usage);

for await (const event of agent.stream("What time is it?")) {
  if (event.type === "token") process.stdout.write(event.delta);
}
```

## Quickstart (Python)

```python
import asyncio
from pydantic import BaseModel

from framework_agents import Agent, AgentOptions, define_tool, init_chat_model, load_config


class Empty(BaseModel):
    pass


async def get_time(args: Empty, ctx) -> dict:
    return {"time": "2026-01-01T00:00:00Z"}


agent = Agent(AgentOptions(
    model=init_chat_model(f"together:{load_config().together_model}"),
    tools=[define_tool(
        name="get_time",
        description="Returns the current time in ISO format",
        schema=Empty,  # pydantic model = the validated argument shape
        execute=get_time,
    )],
))

result = asyncio.run(agent.run("What time is it?"))
print(result.text, result.usage)

async for event in agent.stream("What time is it?"):
    if event["type"] == "token":
        print(event["delta"], end="")
```

## Features

- **ReAct loop** — model → tool calls → execution → append, with `maxSteps`, per-run state, and a synthesized final answer when steps run out mid-tool-use.
- **Tools** — zod (TS) / pydantic (Python) argument schemas; the schema reaches the provider wire so the model sees real argument shapes.
- **Streaming** — typed `step | token | thinking | tool_call | tool_result | done` events as async iterators.
- **Budgets** — cumulative token, USD-cost, tool-call, and deadline caps enforced after every model call.
- **Structured output** — provider `response_format` or a synthetic self-repairing tool, chosen automatically by provider capability.
- **Memory & sessions** — windowing, compaction, conversation managers, session snapshots, HITL checkpoint/resume.
- **Middleware** — hook the model/tool lifecycle without forking the loop: retries, fallback, PII, HITL, context editing, subagents, and more.
- **Multi-agent** — sequential/parallel/router orchestration, DAG workflows, autonomous swarms, JSONL evals.
- **Semantic graph** — quad store with RDFS/OWL entailment proofs, SHACL-subset validation, JSON-LD I/O, remote SPARQL.
- **A2A & serving** — serve any agent over JSON-RPC + SSE; consume remote agents as clients or tools; `framework.json` + CLI.
- **MCP** — Streamable HTTP and stdio clients with tool bridging.
- **React** — a `useAgent` hook that streams turns, thinking, and tool activity into component state.

## Where to go next

- [Getting started: TypeScript](getting-started/typescript.md)
- [Getting started: Python](getting-started/python.md)
- [Live examples (both languages)](getting-started/live-examples.md)
- [Agents & tools](guides/agents-and-tools.md)
- [Streaming & events](guides/streaming.md)
- [Structured output](guides/structured-output.md)
- [Budgets & pricing](guides/budgets.md)
- [Memory & sessions](guides/memory-sessions.md)
- [Middleware & HITL](guides/middleware.md)
- [Multi-agent](guides/multi-agent.md)
- [Semantic graph](guides/semantic.md)
- [A2A & serving](guides/a2a-serving.md)
- [MCP clients](guides/mcp.md)
- [React (frontend)](guides/react.md)
- [Sandbox & rag & skills](guides/sandbox-rag-skills.md)
- [Design docs](design/index.md)