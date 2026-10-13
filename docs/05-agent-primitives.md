# 05 — Agent Primitives (mental models)

## Agent
An async loop with a budget. Owns: system prompt, model, tools, memory, maxSteps.
Never calls LLMs directly — always through `ModelProvider`. Never runs tools without a schema.

## Tool
`{ name, description, schema, execute }`. The schema IS the contract: what the model may send,
what gets validated, what TypeScript infers. Prefer 3–8 narrow tools over 1 god-tool.
`ToolRegistry` is the only lookup path in production code (not `Array.find`).

## Memory
`load → prompt → save` window. `InMemoryMemory` keeps last N messages; persistent adapters (P1)
keep the same interface. System prompts never persist; tool outputs persist as `role: tool`.

## ModelProvider
`complete({ messages, tools }) → { text, toolCalls, usage }`. Providers translate this to
OpenAI/Anthropic/etc. wire formats. `MockModelProvider` returns deterministic text for tests.

## Runtime
Cancellation (`AbortSignal`), timeouts (`timeoutMs`), and later token/cost/tool-call budgets.
All enforcement lives here so Agent/Orchestration/Workflows share semantics.

## Observability
`AgentEvents { onStep, onToolCall, onToolResult, onDone }` (+ `onToken` in P0-streaming).
Core emits; adapters export (console now, OTel/Langfuse in P3). Never `console.log` inside the loop —
emit and let the host decide.
