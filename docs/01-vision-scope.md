# 01 — Vision & Scope

## Vision
A small, composable, observable platform for **building with agents**: single agents that use tools,
then teams of agents wired into workflows — with the same primitives in TypeScript first, Python later.

## Who it's for
- Builders shipping agent features inside TS apps (API routes, edge, CLI).
- Later: Python services needing the same Agent/Tool/Memory semantics.

## Non-goals (v0)
- No framework lock-in (no forced vector DB, no forced LLM vendor).
- No hidden magic: explicit loop, explicit tool schemas, explicit budgets.
- No hosted runtime / control plane in v0 — library first, services later (P3).

## Success criteria
- `new Agent({ model, tools, memory }).run("...")` works in < 20 lines.
- Tools are zod-validated; unknown tools / bad args fail loudly with codes.
- Every step/tool call is observable via `AgentEvents`.
- `MockModelProvider` lets you test without network or keys.
