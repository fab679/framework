# 04 — Features Roadmap (everything to implement)

Legend: `[ ]` todo, `[~]` stubbed in scaffold, `[x]` done. Build P0 top-to-bottom.

## P0 — Usable single agent (v0.1)
- [x] Project scaffold (monorepo, TS package, docs, CI)
- [x] `Agent` ReAct loop with `maxSteps`
- [x] `Tool` + `defineTool` + `ToolRegistry` (zod validation)
- [x] `Memory` interface + `InMemoryMemory` (windowed)
- [x] `ModelProvider` interface + `MockModelProvider`
- [x] `runWithBudget` (AbortSignal + timeout) + `AgentError` codes
- [x] `AgentEvents` hooks + `consoleEvents` (dep-free ANSI colors, NO_COLOR/TTY aware) + `logError`
- [x] Streaming: `stream: true` SSE in provider (`delta.content` → `onToken`, `delta.reasoning_content` → `onThinking`), wired through the agent loop
- [x] Structured errors: `UNKNOWN_TOOL`, `TOOL_ARG_INVALID`, `TOOL_EXECUTION` (tool bugs wrapped, `AgentError` passes through), `MODEL_ERROR` (retryable on 429/5xx/network), `TIMEOUT`, `ABORTED` (no DOMException leaks), `BUDGET_EXCEEDED` (via `maxToolCalls`), `MODEL_CAPABILITY` (images on text-only models), `WORKFLOW_*` / `ROUTER_NO_MATCH` / `ORCHESTRATION_EMPTY` — unit test per code
- [x] Content-block messages: `text`/`reasoning`/`image`/`audio`/`file`/`tool_call`/`non_standard`, ids, factories, zod-validated serialization round-trip
- [x] Tool artifacts: `toResult()` splits model-visible content from downstream-only `artifact`
- [x] `outputVersion` v0/v1: v1 preserves thinking as `reasoning` blocks in history
- [x] `prompts/` — `definePrompt` + file loading + A/B variants
- [x] `skills/` — `SKILL.md` frontmatter parse + `SkillRegistry` + FS discovery + TF-IDF routing
- [x] OpenAI-compatible provider (`fetch`-based, no SDK lock-in) — live-verified on TogetherAI (`E2E_MODEL=1`), mock tests offline-green
- [x] `.env` + dotenv `loadConfig()` (TogetherAI preferred, OpenAI fallback; never commit keys)
- [x] Examples: all run green (`01,02,03,04,06,08,09,10,11,12,13,14,15,16` — including sessions, swarm/a2a, providers, HITL resume)
- [x] Coverage gate >= 80% (`test:coverage` enforces lines/functions/branches/statements)

## P0-sem — Semantic grounding, read path (v0.1-sem, with P0)
- [x] `semantic/` — `GraphStore` + `InMemoryGraphStore`: RDFS + OWL (transitive/symmetric/inverse/functional-conflict) to fixpoint, SHACL-subset shapes, versioned CONSTRUCT rules, `why()` proofs, dedup, export/import
- [x] `ontology_lookup` + `sparql_select` (read-only) + `graph_add` (autonomous validated writes) + `claim_check` (grounded/unknown/contradicted) tools over the closure
- [x] Provenance rule enforced: agent writes without `dct:source` rejected
- [x] Example: `06-semantic-graph.ts` (seed Turtle → query closure → cite proof)

## P1 — Teams + reliability (v0.2)
- [x] Orchestration: `sequential` (output→input chaining), `parallel` (concurrency cap, ordered results), `router` (model-picked specialist, `ROUTER_NO_MATCH` on invalid) — `Agent.description` feeds the router menu
- [x] Workflows: DAG with topological levels, concurrent siblings, per-node retry (backoff), `WORKFLOW_*` errors (duplicate/unknown-dep/self-dep/cycle)
- [x] Swarm: autonomous `handoff_to_agent` coordination, shared context (task/history/knowledge/roster), multimodal task input, `maxHandoffs/maxIterations` + execution/node timeouts + ping-pong detection, typed stream events, `COMPLETED/FAILED` results
- [x] A2A: `A2AClient` (card + send) + `a2aAsTool` + `A2AServer` (card/message/send/SSE `/stream`, per-context isolation, LRU cap, durable HITL threads, CORS, `/ok` health)
- [x] State: `AgentState` (validated JSON KV) + `ToolContext` + per-run `invocationState` (shared by ref, returned on result); `agent.messages` + `initialMessages`; `pause()`/`resume()`; `editMessage` + `branch()` with version timeline; shared across orchestration/workflows via run opts
- [x] Conversation managers: Null / SlidingWindow (window/pinFirst/perTurn, pair-safe trim) / Summarizing (ratio/preserve/model-or-fn)
- [x] Sessions: `InMemoryStorage`/`LocalFileStorage` + `SessionManager` (latest + immutable checkpoints, delete) + `takeSnapshot` (preset/include/exclude/appData) / `loadSnapshot` + auto restore-on-first-run + save-after-run
- [x] MCP: `McpHttpClient` (Streamable HTTP, JSON+SSE) + `McpStdioClient` + `mcpToolsToFramework` (JSON-Schema→zod bridge, collision namespacing) — live-tested against a fake server
- [x] Skills discovery: `discoverSkills` (SKILL.md scan) + `loadSkillsInto` + `matchSkills` ($mention + overlap routing)
- [x] Middleware: hooks (`beforeModel/wrapModel/afterModel/beforeTool/aroundTool/afterTool`: before_* in order, after_* reversed, wrap_* nested) + `createMiddleware` + middleware-contributed tools
- [x] Prebuilt middleware: full `toolRetry`/`modelRetry` (backoff+jitter/filter/onFailure) + `modelFallback`, `modelCallLimit`, `toolCallLimit`, `humanInTheLoop` (approve/edit/reject), `pii` (redact/mask/hash/block + custom detectors), `summarization`, `contextEditing` (ClearToolUses), `todoList` (`write_todos`), `llmToolSelector`, `toolEmulator`, `filesystem` (ls/read/write/edit, root-jailed), `subagents` (`task` tool)
- [x] `stampRetryable`/`getRetryable` verdicts; `initChatModel("provider:model")` (together/openai/anthropic/ollama/custom)
- [x] Structured output: `toolStrategy`/`providerStrategy` (`responseFormat`), `structuredResponse` on result, in-loop repair
- [x] Event streaming: `agent.stream()` typed events + `collectStream`
- [x] Compaction: `CompactedMemory` + model summarizer + semantic (keep-triples) variant + fidelity tests
- [x] Budgets: `maxTokens`, `maxCostUsd` (+prices), `maxToolCalls`, `deadlineMs` enforced per model call
- [x] Retries: exponential backoff with jitter for `retryable` errors (verdicts honored); per-tool/model retry middleware
- [x] Persistent memory: `FileMemory` (atomic JSON, validated reload); Redis/Postgres stay adapters
- [x] Tokenizer: dependency-free word/CJK/punctuation estimate (documented approximation) + truncation in memory
- [x] Evals harness: `eval/` JSONL datasets + scorer + pass-rate reporter (`pnpm eval`, `EVAL_LIVE=1` for live); 4 seed evals green

## P1-sem — Semantic writes + rules (v0.2-sem, with P1)
- [x] OWL property axioms: transitive / symmetric / inverse / functional (functional conflict = violation, not second fact)
- [x] SHACL-subset engine: class shapes (min/max/datatype/nodeKind/hasValue), violations → repair feedback → model self-repair loop
- [x] Versioned CONSTRUCT-style rules to fixpoint; every inferred quad keeps `prov:wasDerivedFrom` + rule id
- [x] `GraphMemory implements Memory`: `load()` injects relevant closure slice, `save()` delegates, `exportState/importState` for session LTS

## P2 — Ecosystem (v0.3)
- [x] Providers: Anthropic native API (text/thinking/tool_use/SSE, MODEL_ERROR mapping) + local Ollama (`ollama()`, no key) + `initChatModel` prefixes (together/openai/anthropic/ollama/custom)
- [x] `framework.json` + `framework-serve` CLI (`dev/serve|up/validate`, `--port/--host/-c`): agent files + overrides, framework/agent `envFile` + inline `env{}`, checkpointer dir, CORS, `/ok` health
- [x] Frontend: `AgentClient` (send + SSE stream) + `handleAgentRoute` (Next.js/TanStack Start bridge) + `framework-agents-react` `useAgent` (turns/thinking/activity/status/send/cancel/reset)
- [x] Checkpointed HITL resume (local-first): `FileCheckpointer` + `withFileLock` (stale reclaim) + `HitlInterrupt`/`hitlKey` + `agent.resume()`; S3 backends and distributed locks explicitly deferred (third-party)
- [x] Local skill routing: dependency-free TF-IDF cosine (`scoreSkills` seam for future embedders)
- [x] Provider fallback chain via `modelFallbackMiddleware(...providers)` (any backends)
- [x] RAG interface: `Retriever { search(query, k) }` + `retrievalTool()` + TF-IDF + SPARQL adapters (no bundled vector DB)
- [x] Human-in-the-loop: approve/edit/reject/suspend + checkpointed resume (`agent.resume`, `hitlKey`)
- [x] Guardrails: PII middleware, output schema enforcement (`responseFormat`), tool sandboxing via emulator
- [x] Sandbox: `run_javascript` (vm, timeout) + `createSandboxBackend` (`execute` shell w/ scrubbed env, ls/read/write/edit/glob/grep, spill-to-file)
- [x] Prompt management: markdown files + `render(vars)` + A/B variants with tests
- [x] Python parity: `framework_agents` (packages/python) — full TS module map ported with pydantic + httpx + aio stdlib; the FIXED semantics are encoded from day one (see packages/python/README.md pinned-semantics table); 195 offline tests + live Together parity (same e2e scenarios as TS) + executable cross-language conformance (Python A2A client ↔ TS A2A server on the real wire)

## P2-sem — Linked data at scale
- [x] Remote SPARQL adapter (SELECT/UPDATE over HTTP, auth, prefixes) — `RemoteSparqlAdapter` + `remoteSparqlTool` (SELECT always; UPDATE opt-in)
- [x] Entity linking tool (`mention → IRI` via sameAs index) + JSON-LD expand/compact I/O — `EntityLinker`/`linkEntityTool`, autonomous-by-default `MappingProposals` (review = `autonomy: false`), `quadsToJsonLd`/`jsonldToQuads` with named-graph round-trip
- [x] Ontology evolution: TBox v3→v4 migration + conformance battery — `migrateTBox` (rebuild; v3 untouched) + `runConformance`; each version's TBox lands in its own named graph (`urn:framework:ontology:<to>`)

## P3 — Production hardening (v0.4+)
- [x] Observability exporters: OTel-shaped spans (console/JSONL/memory sinks) behind `AgentEvents`; vendor HTTP adapters stay out
- [ ] Tool/skill registry service: namespaced packages, semver, signed manifests
- [ ] Distributed runtime: queue-backed runs, idempotency keys, resume-from-checkpoint
- [x] Guardrails: `policyFilterMiddleware` (allow/deny, continue/error) + PII + schemas + emulator
- [x] Benchmarks: `bench/` token/latency harness + `--baseline` regression gate (`pnpm bench`)

## Out of scope (explicitly not building yet)
- Hosted UI / playground, hosted API, billing — library first.
- Fine-tuning pipelines, model training.
- External internet search (web_search provider + fetch/extract) — deferred, out of scaffold for now.
