# Framework Agents — Agentic Platform

Monorepo for building with agents. TypeScript-first, Python at API parity, both
live-tested against the same provider. **Python has shipped** — the full module
map is ported with cross-language conformance (the wire contract is identical).

```
framework/                  <- you are here (monorepo root)
├── docs/                   <- developer docs (MkDocs — GitHub Pages ready)
├── mkdocs.yml
├── packages/
│   ├── typescript/         <- npm: framework-agents (+ framework-agents-react)
│   └── python/             <- PyPI: framework-agents
├── .github/workflows/      <- docs deploy (GitHub Pages)
└── examples live folders: packages/*/examples/
```

## Developer docs

The `docs/` folder IS the MkDocs site (Material theme):

```bash
python3 -m venv .venv-docs && .venv-docs/bin/pip install -q mkdocs mkdocs-material
.venv-docs/bin/mkdocs serve             # http://localhost:8000
.venv-docs/bin/mkdocs build --strict    # CI builds exactly this
```

Publishing ( See `docs/reference/publishing.md` when the docs site is built —
full details):

```bash
# npm (build first)
pnpm --filter framework-agents build && (cd packages/typescript && npm publish)
pnpm --filter framework-agents-react build && (cd packages/react && npm publish)
# PyPI
(cd packages/python && .venv/bin/python -m build && twine upload dist/*)
```

## Packages

| Registry | Name | Source |
|---|---|---|
| npm | `framework-agents` | `packages/typescript` |
| npm | `framework-agents-react` | `packages/react` |
| PyPI | `framework-agents` | `packages/python` |

```bash
# npm (build first)
pnpm --filter framework-agents build && (cd packages/typescript && npm publish)
pnpm --filter framework-agents-react build && (cd packages/react && npm publish)
# PyPI
(cd packages/python && .venv/bin/python -m build && twine upload dist/*)
```

## Quickstart (TypeScript)

```bash
pnpm install
pnpm --filter framework-agents build
pnpm --filter framework-agents test
```

Run the basic example:

```bash
pnpm --filter framework-agents dev -- examples/01-basic-agent.ts
```

Live examples (real Together.ai calls, `TOGETHER_API_KEY` in `.env`) exercise
every feature — streaming with live tool events, orchestration, swarms,
workflows, memory/sessions, HITL resume, guardrails, the semantic graph,
A2A over the wire, and retrieval: `packages/typescript/examples/live/`.

## Quickstart (Python)

```bash
cd packages/python
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest tests -q
cd examples && ../.venv/bin/python 01-streaming-tools.py
```

## Docs map (design docs, source material)

| Doc | What |
|-----|------|
| `docs/01-vision-scope.md` | Why this exists, non-goals |
| `docs/02-architecture.md` | Layer diagram + module map |
| `docs/03-typescript-package-spec.md` | Public API contract for the TS package |
| `docs/04-features-roadmap.md` | Full feature checklist to implement (P0–P3) |
| `docs/05-agent-primitives.md` | Agent / Tool / Memory / Model concepts |
| `docs/06-decisions-adr-001-monorepo.md` | Why monorepo + TS-first |
| `docs/07-semantics-governance.md` | Semantic-layer ownership rules |
| `docs/08-semantics-playbook.md` | Semantic lifecycles in practice |

## Engineering standards

- Node >= 20, pnpm 9, Turbo, strict TypeScript; Python >= 3.11, pydantic + httpx
- `tsup` for builds (ESM + d.ts), `vitest` for tests, `eslint` flat config
- Conventional Commits, CI runs `lint + typecheck + test + build`
- No secrets in repo (`.env.example` only)
- Live tests hit the real Together.ai API only when gated: `E2E_MODEL=1 pnpm --filter framework-agents test -- --run tests/e2e/together.live.test.ts`