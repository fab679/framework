# Live examples — real Together.ai API, every feature

Every file here hits the **real** API (TogetherAI only, per `.env`).
Cost is cents per file (Qwen3.5-9B, bounded `maxTokens`). Not for CI —
offline examples (`examples/*.ts`) use deterministic stubs instead.

```bash
# from packages/typescript
pnpm dev -- examples/live/01-streaming.ts     # any file
for f in examples/live/*.ts; do pnpm dev -- "$f"; done
```

**Network resilience:** every live agent wraps `modelRetryMiddleware({ maxRetries: 3 })` —
transient 429/5xx/socket failures retry with exponential backoff + jitter
instead of killing the demo. Non-retryable errors (budget caps, thinking-past-
`max_tokens` truncation with no content) surface immediately, per their
`stampRetryable`/provider verdicts.

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