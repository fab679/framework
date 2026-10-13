# Agents & tools

One mental model, two languages. TypeScript first, then the same headings for Python — names map `camelCase` → `snake_case` unless noted.

## TypeScript

### Agent construction

`new Agent(options: AgentOptions)` — fields:

- `model: ModelProvider` — required. Built by `initChatModel("provider:model")` (see [getting started](../getting-started/typescript.md)) or directly (`OpenAICompatibleProvider`, `AnthropicProvider`, `OllamaProvider`, `MockModelProvider`).
- `tools?: Tool[]` — schema-validated tools (below). Middleware-contributed tools are merged at construction: prepended, deduplicated by name, first occurrence wins.
- `systemPrompt?: string` — default `"You are a helpful agent. Use tools when needed. Be concise."`.
- `maxSteps?: number` — default `6`. Exhaustion mid-tool-use synthesizes and stores a final assistant message (history never ends on a dangling tool call).
- `maxToolCalls?: number` — per-run tool execution cap; exceeded throws `BUDGET_EXCEEDED`.
- `budgets?: TokenBudgets` — see [budgets](budgets.md).
- `responseFormat?: ResponseFormat` — see [structured output](structured-output.md).
- `middleware?: AgentMiddleware[]` — see [middleware](middleware.md).
- `memory?: Memory` — see [memory & sessions](memory-sessions.md).
- `initialMessages?: ChatMessage[]` — seed history (loses to a session restore).
- `state?: Record<string, unknown> | AgentState` — key-value state, never sent to the model.
- `invocationState` is per-run (a `run`/`stream` option in TS).
- `conversationManager?`, `sessionManager?`, `events?`, `outputVersion?` (`"v0"` plain text turns, `"v1"` also stores thinking as reasoning blocks), `name?`, `description?`.

Useful members: `agent.run(input, opts?)` returns an `AgentResult` (`text`, `messages`, `toolCalls`, `usage`, `invocationState`, `structuredResponse?`); `agent.stream(...)` yields typed events; `agent.withTools(...extra)` clones with appended control tools (memory/state are fresh per clone); `agent.state`, `agent.messages`, `agent.timeline`. First-class signal: pass `{ signal: AbortSignal }` in `RunOptions` — the abort propagates into the in-flight provider request.

### defineTool + parameters schemas

```ts
import { defineTool } from "framework-agents";
import { z } from "zod";

const getWeather = defineTool({
  name: "get_weather",
  description: "Current weather for a city",
  schema: z.object({ city: z.string() }),             // zod v3 or v4 — schema field is version-agnostic
  execute: async ({ city }: { city: string }, ctx) => ({ city, tempC: 21 }),
});                                              // ^ annotate execute's arg once: that type IS TArgs
```

The zod `schema` is converted once per tool (zod-to-json-schema, `$refStrategy: "none"`) into a `parametersSchema` that rides the `tools` wire field. This is deliberate: descriptions alone leave argument shapes to guesswork — without `parameters` an OpenAI-compatible API advertises a zero-arg tool. Optional `toResult` splits raw output into model-visible `content` and downstream-only `artifact` (default: JSON/string content, no artifact) — reunion metadata, debug blobs, etc. stay out of the model's context while remaining on the `toolCalls` record.

### ToolRegistry

A validating registry — one place where tools are looked up and argument-checked:

```ts
const registry = new ToolRegistry().register(getWeather);
await registry.call("get_weather", { city: "Tokyo" }); // parses via the zod schema
// Unknown name → UNKNOWN_TOOL; bad args → TOOL_ARG_INVALID; both non-retryable
```

A second `ToolContext` argument may be passed to `call` so registry calls don't force `ctx` to be `undefined` for tools that read state.

### ToolContext

`execute(args, ctx?)` — when the agent loop dispatches, `ctx` is `{ agentName, state, invocationState }`:

- `state`: the agent's `AgentState` (persistent key-value state, never sent to the model)
- `invocationState`: the LIVE per-run dict/object, shared by reference across hooks and tools in one run (and across a HITL suspend/resume)

### events hooks

`AgentEvents` (also wired to `consoleEvents()` for a dev logger): `onStep({step, messages})`, `onToken(delta)`, `onThinking(delta)`, `onToolCall({tool, args})`, `onToolResult({tool, output})`, `onDone({text, toolCalls})`. `run()` and `stream()` fire them identically.

### Unknown-tool & failure semantics

- Unknown tool name → the loop appends a `role: "tool"` message with `"Unknown tool: <name>"` and keeps going; the model self-corrects.
- `ToolRejection` (thrown by middleware `beforeTool`/`aroundTool`) → its `feedback` string becomes the tool output; the run continues.
- Any other tool error → wrapped as `AgentError("TOOL_EXECUTION")` (non-retryable) after hooks run.

## Python

### Agent construction

`Agent(AgentOptions(...))` — fields: `model` (required), `name`, `description`, `system_prompt`, `tools` (`list[Tool]`), `memory`, `max_steps` (default 6), `max_tool_calls`, `budgets` (`TokenBudgets`), `response_format`, `events` (`AgentEvents` dataclass), `initial_messages`, `middleware` (contributed tools prepended, dedupe by name, first wins), `state` (`AgentState` or dict), `invocation_state` (default per-run state can also live on the options; a `run`/`stream` kwarg overrides it), `session_manager`, `output_version` (`"v0"`/`"v1"`).

Note: TS models `invocationState` as a `RunOptions` argument; Python accepts it both on `AgentOptions.invocation_state` and as a `run`/`stream` kwarg (kwarg wins).

Same members, snake_case: `await agent.run(input_text, ...)`, `agent.stream(...)` (an `AgentStream` — see [streaming](streaming.md)), `agent.with_tools(...)`, `agent.state`, `agent.messages`, `agent.timeline`, `agent.session_id`. `run()` drains the stream; concurrent streams each capture their own result.

### define_tool + parameters schemas

```python
from pydantic import BaseModel

class Weather(BaseModel):
    city: str

async def get_weather(args: Weather, ctx) -> dict:
    return {"city": args.city, "tempC": 21}

weather = define_tool(
    name="get_weather",
    description="Current weather for a city",
    schema=Weather,
    execute=get_weather,
)
```

The pydantic model is serialized with `model_json_schema()` via `parameters_schema(tool)` and sent in `ToolWire.parameters_schema` on the wire. `to_result` (async callable → `ToolResult`) is optional, same content/artifact split as TS.

### ToolRegistry

`ToolRegistry().register(tool)`; `await registry.call("get_weather", {"city": "Tokyo"}, ctx?)` validates with `tool.schema.model_validate` — unknown name raises `UNKNOWN_TOOL`, bad args `TOOL_ARG_INVALID`.

### ToolContext

`ToolContext(agent_name, state, invocation_state)` — the loop wires the agent's `AgentState` and the live per-run `invocation_state` by reference, exactly like the TS `toolCtx`.

### events hooks

`AgentEvents(on_step, on_token, on_thinking, on_tool_call, on_tool_result, on_done)` — plain callables (async or sync). `console_events()` is the dev logger:

```python
from framework_agents import AgentOptions
from framework_agents.observability import console_events
```

### Unknown-tool & failure semantics

- Unknown tool → `"Unknown tool: <name>"` tool message, run continues.
- Invalid args (pydantic `ValidationError`) → `"Invalid args for tool ..."` tool message as repair feedback, run continues — the loop re-validates args before every execution, including after a `before_tool` hook edits a call.
- `ToolRejection` → `feedback` becomes the output; `HitlInterrupt` passes through for checkpoint/resume; everything else wraps into `AgentError("TOOL_EXECUTION", retryable=False)`.

## Next

- [Streaming & events](streaming.md)
- [Structured output](structured-output.md)
- [Middleware & HITL](middleware.md)