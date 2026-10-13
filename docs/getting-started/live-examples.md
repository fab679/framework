# Live examples (both languages)

Both packages ship runnable demos against the **real TogetherAI API** — the only provider the default configuration is tested against (`TOGETHER_API_KEY` in the repo root `.env`). Cost is cents per file (small chat models, bounded `maxTokens`). They are not for CI; the offline unit tests and TS examples use deterministic stubs.

Prerequisites: for TypeScript, install workspace deps in `packages/typescript` (`pnpm install`, plus `tsx` from devDependencies); for Python, the editable install with dev extras (see [getting started: Python](python.md)). Both folders are also readable as a feature tour — each file names the one feature it exercises, in the same order as the guides.

**Retry coverage:** every live agent wraps a model-retry middleware — `modelRetryMiddleware({ maxRetries: 3 })` in TS, `model_retry(max_retries=3)` in Python — so transient 429/5xx/socket failures retry with exponential backoff + jitter instead of killing the demo. Non-retryable errors (budget caps, truncation-with-no-content) surface immediately, per their stamped/`retryable` verdicts.

## TypeScript — `packages/typescript/examples/live/`

Run from `packages/typescript`:

```bash
pnpm dev -- examples/live/01-streaming.ts     # any file
for f in examples/live/*.ts; do pnpm dev -- "$f"; done
```

| File | Feature under test |
|---|---|
| `01-streaming.ts` | `agent.stream()` events, thinking deltas live, streaming-tolerant middleware |
| `02-orchestration.ts` | sequential chain, parallel fan-out (concurrency), model router |
| `03-swarm.ts` | autonomous handoffs, typed swarm stream events, loop detection |
| `04-workflows.ts` | DAG levels, `dependsOn`, per-node retry with real failures |
| `05-memory-sessions.ts` | multi-turn memory, compaction coupon, session restore across "restart" |
| `06-hitl-resume.ts` | human-in-the-loop suspend → checkpoint → resume with a real model call |
| `07-guardrails-budgets.ts` | PII redaction, policy deny → model self-corrects, live cost budgets |
| `08-semantic-graph.ts` | validated graph writes, claim_check, ontology self-review, SHACL induction |
| `09-a2a-remote.ts` | A2A server + client + a2aAsTool (a live agent calling a live remote agent) |
| `10-rag-retrieval.ts` | TF-IDF retrieval grounding a live answer + subagent middleware |
| `11-conversation-managers.ts` | SlidingWindow in-loop trimming, live-model Summarizing, GraphMemory fact injection |
| `12-middleware-suite.ts` | todoList, toolCallLimit feedback, modelCallLimit, modelFallback failover, llmToolSelector |
| `13-sandbox-execution.ts` | Live model running real shell commands + JS in the jailed workspace |
| `14-skills-prompts.ts` | SKILL.md discovery + $mention routing + prompt templates + A/B variants |
| `15-evaluation-harness.ts` | JSONL dataset + live scored evals + pass-rate reporter |
| `16-mcp-bridge.ts` | Real stdio MCP server child process, bridged tools, live model driving them |
| `17-ontology-evolution.ts` | Live model writes facts; migrateTBox v3→v4 with battery + named TBox graph |
| `18-tracing-observability.ts` | OTel-shaped spans to memory + JSONL sinks, console events, live run |
| `19-framework-serve.ts` | framework.json → serveFramework boot → remote A2A wire → clean shutdown |
| `20-remote-sparql.ts` | RemoteSparqlAdapter against the public Wikidata endpoint (SELECT, retry, UA) |
| `21-branching.ts` | branch() forks a live conversation: shared context, independent divergence, timeline + editMessage |

## Python — `packages/python/examples/`

Run from `packages/python` (after `pip install -e ".[dev]"`):

```bash
for f in examples/*.py; do .venv/bin/python "$f"; done   # each ~cents
```

| File | Live feature |
|---|---|
| `01-streaming-tools.py` | stream events, thinking deltas, tool calls streamed mid-run |
| `02-orchestration.py` | sequential chain + parallel fan-out + model-picked router |
| `03-swarm.py` | autonomous handoffs, typed swarm events, loop detection |
| `04-workflows.py` | DAG levels, dedup'd deps, real node retry |
| `05-memory-sessions.py` | multi-turn memory, session restore across restart |
| `06-hitl-resume.py` | suspend → checkpoint → wildcard-approve → resume |
| `07-guardrails-budgets.py` | PII redaction, policy deny, budget caps on real usage |
| `08-semantic-graph.py` | validated graph writes, self-review, SHACL adoption |
| `09-a2a-remote.py` | A2A server + client + remote-agent-as-tool |
| `10-middleware-suite.py` | todos, tool budgets, failover, tool subset selection |
| `11-rag-retrieval.py` | TF-IDF retrieval grounding a live answer |
| `12-providers.py` | provider factory (together/openai/anthropic/ollama) + live cost |

## Live-cost lesson (chained prompts)

Thinking models can burn their whole `maxTokens` budget on reasoning when prompts are chained (e.g. orchestration hand-offs): the run then ends at `finish_reason=length` with zero content, which the providers turn into a loud, non-retryable `MODEL_ERROR` instead of a silent empty answer. The live orchestration demos set `maxTokens: 4000` for exactly this reason — see [`examples/live/02-orchestration.ts`](../guides/budgets.md) and [streaming](../guides/streaming.md) for the guard details.

## Related guides

- [Streaming & events](../guides/streaming.md)
- [Multi-agent](../guides/multi-agent.md)
- [A2A & serving](../guides/a2a-serving.md)