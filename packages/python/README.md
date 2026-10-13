# Python package — TS parity port (`framework_agents`)

The TypeScript package (`packages/typescript`) is the reference implementation.
This port targets API parity with the **fixed** TS semantics — every behavior
that was a real bug during the TS line (and is regression-pinned there) is
encoded here from day one:

| Pinned semantic | Why |
|---|---|
| `reasoning_tokens` is a SUBSET of `output_tokens` | never double-billed in budgets/cost |
| `cost_usd` only when a price is known | unknown models → `None`, never a fake $0 |
| `save()` APPENDS; the loop does clear-then-save | history never duplicates across runs |
| Structured `'auto'` resolves by capability | unresolved auto never throws STRUCTURED_NO_MATCH |
| `finish_reason=length` + zero content → loud `MODEL_ERROR` | a thinking model burning its whole budget is not a silent empty answer |
| Tool parameter schemas ride the wire | models see real argument shapes |
| HITL decisions: exact key THEN bare tool name | resume converges even when models regenerate arg text |
| Local `ollama:` pricing wins over name collisions | never bill cloud prices for free local models |
| Storage keys/threadIds reject traversal (`..`) | no writes outside the root |
| Remote SPARQL: descriptive UA + 429/5xx retry honoring Retry-After | public endpoints reject and rate-limit otherwise |

No personal or brand names appear anywhere in this package.

## Port status (mirroring the TS module map)

| TS module | Python | State |
|---|---|---|
| types (messages/blocks/usage) | `types.py` | ✅ |
| errors + retryable verdicts | `errors.py` | ✅ |
| tools (define_tool/registry/schemas) | `tools.py` | ✅ |
| memory (+compaction) | `memory.py`, `memory_compaction.py` | ✅ (MEMORY_CORRUPT loud, atomic unique-tmp writes) |
| conversation managers | `conversation.py` | ✅ (order-preserving pin; model or fn summarizer) |
| sessions + snapshots | `sessions.py` | ✅ (validated keys, TS-tested preset semantics) |
| checkpoint + file locks | `checkpoint.py` | ✅ (O_EXCL + stale reclaim, threadId validation) |
| runtime (abort/timeout) | `runtime.py` | ✅ |
| models (mock + OpenAI-compatible + Anthropic + Ollama) | `models/` | ✅ (Together+OpenAI field unions, native Anthropic Messages API w/ thinking budgets + SSE machine, vision capability fail-fast, stream usage, truncation guard) |
| state (AgentState + invocation) | `state.py`, `agent.py` | ✅ (validated JSON KV, custom-prototype rejection, snapshots/session restore, pause/resume, edit/branch/timelines, v1 reasoning blocks, `with_tools`) |
| pricing | `pricing.py` | ✅ |
| config + factory | `config.py` | ✅ (together/openai/ollama/custom) |
| agent loop (budgets/structured/streaming) | `agent.py` | ✅ (calls stream via drain; budget price refusal; synthesized final answer) |
| middleware (hooks, HITL, retries, limits, policy, todos, pii) | `middleware.py` | ✅ |
| orchestration | `orchestration.py` | ✅ (sequential/parallel/router; routed result semantics) |
| workflows | `workflows.py` | ✅ (dedup'd indegree, retries, propagation) |
| swarm | `swarm.py` | ✅ (handoff tool + stream events + loop detection default) |
| evals | `evals.py` | ✅ (JSONL + pass-rate reporter) |
| skills + TF-IDF routing | `skills.py` | ✅ ($mention word-boundary wins) |
| prompts (+A/B) | `prompts.py` | ✅ (CRLF/BOM tolerance) |
| rag (tf-idf retriever + tool) | `rag.py` | ✅ |
| semantic (store/SHACL/rules/graph memory/linking/JSON-LD/evolution) | `semantic.py`, `ontology_review.py` | ✅ (version graphs under `urn:framework:ontology:*`) |
| remote SPARQL | `remote_sparql.py` | ✅ (UA + Retry-After retry) |
| A2A clients (SSE + JSON-RPC) | `a2a_client.py` | ✅ (envelope + contextId chain + retry) |
| A2A server (contexts/LRU/disconnect/SSE) | `a2a_server.py` | ✅ (stdio-of-asyncio raw HTTP, body caps, CORS) |
| MCP clients + bridging | `mcp_client.py` | ✅ (stdio child lifecycle, no-id notification, per-tool degrade) |
| observability + tracing sinks | `observability.py`, `tracing.py` | ✅ |
| sandbox (JS + shell) | `sandbox.py` | ✅ (caveat: exec timeouts abandon a thread, no hard kill) |
| serve (framework.json + CLI) | `serve.py` | ✅ (lazy model init, argparse CLI, per-context isolated agents) |
| React SDK | — | n/a (React-specific; browser clients use A2A/SSE) |

**Batch-2 middleware** (`model_fallback`, `summarization`, `context_editing` +
`ClearToolUsesEdit`, `llm_tool_selector`, `tool_emulator`, `filesystem`,
`create_sub_agent`, `pii` tool-results) is ported with deviations pinned in
`middleware.py`'s docstring (feedback-strings instead of raises on escape
misses; `older_than_steps` interpreted as a step gate).

## Running

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest tests -q                 # offline parity suite (233 tests)
E2E_MODEL=1 .venv/bin/python -m pytest tests -q -m live
# live Together parity runs; OpenAI + Anthropic suites skip cleanly and go
# live as soon as OPENAI_API_KEY / ANTHROPIC_API_KEY appear in framework/.env
```

## Known deviations (documented in module docstrings)

- `Agent.stream()` yields a `done` event and stores the result (generators
  can't return values); run() drains — wire contracts identical.
- Async file IO runs via `asyncio.to_thread` in filesystem tools; storage
  classes use blocking IO inside async methods (single-process locality).
- Sandbox JS cannot hard-interrupt a thread mid-statement (node:vm can) —
  the timeout surfaces per contract, the abandoned thread dies at exit.
- Semantic version graphs use `urn:framework:ontology:<version>` (the TS
  default `urn:fabisch:*` embeds a personal name; both are configurable).
- Cross-language conformance: the Python A2A client speaks the exact wire
  contract of the TS A2A server (envelope, SSE frames, [DONE]) and vice
  versa — see `tests/test_servers_and_clients.py`.