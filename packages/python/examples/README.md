# Python live examples — real Together API (TOGETHER_API_KEY in root .env)

Run from packages/python (after `pip install -e ".[dev]"`):

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