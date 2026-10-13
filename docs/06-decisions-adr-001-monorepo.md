# ADR-001 — Monorepo + TypeScript-first

Date: 2026-10-12 · Status: accepted

## Context
Two language packages planned (TypeScript + Python), one shared design. Need a layout that
keeps the TS package shippable now without blocking Python parity later.

## Decision
- `framework/` is the monorepo root (parent folder): docs, shared configs, `turbo`, `pnpm` workspaces.
- `packages/typescript/` is the reference implementation and current focus.
- `packages/python/` is a spec'd placeholder until TS P0 ships.
- `docs/` lives at the root so both languages read the same contracts.

## Consequences
- TS ships first; Python ports `docs/03-typescript-package-spec.md` (not re-designed).
- Shared tooling (lint/typecheck/test/build) runs from root via Turbo.
- Python is NOT in pnpm workspaces (uv-managed when it starts).

# ADR-002 — zod + tsup + vitest (TS)

Date: 2026-10-12 · Status: accepted

- `zod`: single runtime dep; tool schemas double as validation + types.
- `tsup`: ESM + d.ts with zero-config; Node20 target.
- `vitest`: fast unit tests, v8 coverage, E2E flag-gated live tests.
- `eslint` flat config minimal for v0; `prettier` for formatting.
