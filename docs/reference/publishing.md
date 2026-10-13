# Publishing (npm / PyPI)

Three artifacts ship from this monorepo — two on npm, one on PyPI — and all
three are currently **version 0.1.0**. The two language packages share a
wire contract (A2A over HTTP/SSE), so they must be bumped and released
together.

| Registry | Name | Source | Build |
|---|---|---|---|
| npm | `framework-agents` | `packages/typescript` | tsup (ESM + d.ts → `dist/`) |
| npm | `framework-agents-react` | `packages/react` | tsup (ESM + d.ts → `dist/`) |
| PyPI | `framework-agents` | `packages/python` | hatchling (`python -m build` → `dist/`) |

## Before you publish: fix the placeholders

The `repository.url` in **all three** packaging manifests still points at
`https://github.com/fab679/framework` (and pyproject's
`Homepage`/`Documentation` URLs at `fab679.github.io`). Replace them with
the real GitHub org once it exists. The same goes for `site_url` / `repo_url`
in `mkdocs.yml` — the GitHub Pages workflow publishes to wherever that
points, and until it is set the site URL advertised in the page metadata is
wrong.

## npm — `framework-agents`

```bash
cd packages/typescript
pnpm build        # tsup → dist/ (index.js + index.d.ts + cli.js)
npm publish
```

From the monorepo root the equivalent is:

```bash
pnpm --filter framework-agents build && (cd packages/typescript && npm publish)
```

Notes:

- `files` in `package.json` ships `dist`, `README.md`, and `LICENSE` — nothing
  else reaches the registry.
- The build is ESM-only (`"type": "module"`, single entry `dist/index.js`,
  types at `dist/index.d.ts`).
- The package exposes a CLI, `framework-serve` (`dist/cli.js`), which lands in
  the consumer's `bin`.
- Engines: Node `>= 20`.
- `publishConfig.access` is `public`, so an unprefixed scope still publishes
  openly.

## npm — `framework-agents-react`

```bash
cd packages/react
pnpm build        # tsup → dist/
npm publish
```

Same shape as the core package: ESM entry `dist/index.js` plus d.ts, `files`
ships `dist` and `README.md`. Two extra details:

- `react` is a **peer dependency** (`>= 18`) — it is never installed for the
  consumer.
- `framework-agents` is a dependency declared as `"workspace:*"` under pnpm.
  Publish with `pnpm publish` (pnpm rewrites the workspace range to the
  published version at publish time), or verify the range resolves before
  using plain `npm publish` — a literal `workspace:*` on the registry would
  be uninstallable. Either way, `framework-agents` must be published
  **first**; the React hook is built on it.

## PyPI — `framework-agents`

```bash
cd packages/python
.venv/bin/python -m build     # sdist + wheel into dist/
twine upload dist/*
```

Notes:

- Distribution name `framework-agents`; importable package
  `framework_agents` (built from `src/framework_agents`).
- `requires-python = ">=3.11"`; runtime dependencies are exactly
  `pydantic>=2.7` and `httpx>=0.27` — nothing else is needed at runtime.
  (`anyio`, `pytest`, `pytest-asyncio` are dev extras via `.[dev]`.)
- Build backend is `hatchling`; the wheel includes only the package, the
  sdist also carries `tests`, `examples`, and the README.

## Version bump guidance

1. Bump **all three** manifests in the same change: `version` in
   `packages/typescript/package.json`,
   `packages/react/package.json`, and `packages/python/pyproject.toml`
   (all `0.1.0` today).
2. Keep the two language packages in **lockstep** — same version number,
   same commit. The A2A/serve JSON contract is shared: a Python agent serving
   `0.1.1` must speak exactly the wire a TypeScript client at `0.1.1`
   expects (and vice versa) — the MCP `initialize` handshake even carries the
   package version in its `clientInfo` string. Divergent versions would
   silently fork the contract;
   the cross-language conformance suite only guards the versions that exist.
3. The React hook is a pure client over the same contract and can lag the
   core pair by a patch, but there is no reason to: bump it too.
4. Publish order: `framework-agents` (npm) → `framework-agents-react` (npm)
   → `framework-agents` (PyPI). Then tag the release
   (`v<version>`) so the pyproject `Changelog` URL resolves.

## Documentation — GitHub Pages

`.github/workflows/docs.yml` builds and publishes this MkDocs site:

- **Trigger**: any push to `main` that touches `docs/**`, `mkdocs.yml`,
  `packages/**`, or the workflow itself — plus a manual
  `workflow_dispatch`. There is no dedicated `docs-deploy` branch; Pages
  deployment is handled entirely by the workflow's two jobs.
- **Build job**: checkout, Python 3.12, `pip install "mkdocs>=1.6"
  "mkdocs-material>=9.5"`, then `mkdocs build --strict` (a broken nav link
  or unmapped page fails the build), artifact uploaded from `site/`.
- **Deploy job**: `actions/deploy-pages@v4` to the `github-pages` environment.
- **Permissions/concurrency**: `pages: write` plus the `id-token` write for
  the official deploy action; concurrent runs share the `pages` group with
  `cancel-in-progress: true` so only the newest push deploys.

Local preview matches CI:

```bash
python3 -m venv .venv-docs && .venv-docs/bin/pip install -q mkdocs mkdocs-material
.venv-docs/bin/mkdocs serve          # http://localhost:8000
.venv-docs/bin/mkdocs build --strict # exactly what CI builds
```

`mkdocs build --strict` with the current `mkdocs.yml` requires every page in
the `nav` to exist — including this one — so write docs pages before
declaring them in the nav.