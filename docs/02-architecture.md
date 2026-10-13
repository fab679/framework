# 02 — Architecture

## Layer diagram

```
┌─────────────┐   ┌──────────┐   ┌───────────┐   ┌──────────┐
│   Agent     │──▶│  Models  │   │   Tools   │   │  Memory  │
│ (ReAct loop)│   │(provider │   │(registry +│   │(load/save│
│  maxSteps   │   │ iface)   │   │ zod)      │   │ window)  │
└──────┬──────┘   └──────────┘   └───────────┘   └──────────┘
       │                 ▲               ▲
       ▼                 │               │
┌─────────────┐   ┌──────┴───────────────┴───┐
│ Orchestration│  │ Runtime (budget/timeout/ │
│ Workflows    │  │ abort) + Observability   │
│ (P1)         │  │ (events/hooks)           │
└─────────────┘   └──────────────────────────┘
```

## Data flow (P0 loop)
1. `Agent.run(input)` → messages = `[system, ...memory.load(), user]`
2. `model.complete({ messages, tools })` → `{ text }` or `{ toolCalls[] }`
3. For each tool call: `registry.call(name, args)` (zod parse → execute) → append `role: tool` message
4. Repeat until final text or `maxSteps`; `memory.save()`; emit `onDone`.

## Module map → code
| Concept | Code |
|---|---|
| Agent loop | `packages/typescript/src/agent/` |
| Tool + registry | `packages/typescript/src/tools/` |
| Memory | `packages/typescript/src/memory/` |
| ModelProvider | `packages/typescript/src/models/` |
| Runtime/budget | `packages/typescript/src/runtime/` |
| Events/logging | `packages/typescript/src/observability/` |
| Multi-agent | `packages/typescript/src/orchestration/` (P1 stub) |
| DAG workflows | `packages/typescript/src/workflows/` (P1 stub) |

## Key decisions
- ESM-only, Node 20+, strict TS.
- `zod` is the only runtime dep — schemas are the tool contract.
- Core stays LLM-vendor-free; providers are adapters behind `ModelProvider`.
- Observability is an interface (`AgentEvents`), not a logger — exporters come in P2/P3.
