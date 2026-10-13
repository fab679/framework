# Design docs

The design documents live alongside this site in the repository root (`docs/01`–`docs/08`). They are the source of truth for scope, architecture, and pinned semantics — read them in order.

- [01 — Vision & Scope](../01-vision-scope.md) — the "why" and "who": a small, composable, observable platform for building with agents (single agents with tools first, then teams wired into workflows), TypeScript first, Python later.
- [02 — Architecture](../02-architecture.md) — the layer diagram and data flow: Agent (ReAct loop) → Models → Tools (registry + schemas) → Memory, and the module map beneath them.
- [03 — TypeScript Package Spec](../03-typescript-package-spec.md) — the public API contract, the source of truth for the TS package and Python parity (agents, tools, streaming events, provider resolution, budgets).
- [04 — Features Roadmap](../04-features-roadmap.md) — every feature to implement, P0→P3 plus the P0-sem/P1-sem/P2-sem semantic phases, with per-item status (`[ ]` todo, `[~]` stubbed, `[x]` done).
- [05 — Agent Primitives](../05-agent-primitives.md) — mental models: the agent is an async loop with a budget that never calls models directly and never runs tools without a schema; tools, memory, and the rest as first-principles shapes.
- [06 — ADR-001: Monorepo + TypeScript-first](../06-decisions-adr-001-monorepo.md) — the accepted decision on layout: two language packages, one shared design, TS shippable now without blocking Python parity later.
- [07 — Semantics Governance](../07-semantics-governance.md) — who owns meaning: the framework never ships "the ontology," it ships the machinery for governing it (registry → lookup → validation → versioned evolution); content is always user-supplied.
- [08 — Semantics Playbook](../08-semantics-playbook.md) — every way to use the graph: not a database the agent reads but its ground-truth layer — autonomous validated writes, claim checking, and the uses composed into one loop.

These files also anchor the topic guides: the [semantic graph](../guides/semantic.md) page implements the 07/08 lifecycle (enforce → evolve), and the pinned semantics tables in `packages/python/README.md` trace back to the contract in 03.