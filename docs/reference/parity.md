# Cross-language parity

The TypeScript package (`packages/typescript`, npm `framework-agents`) is the
reference implementation. The Python package (`packages/python`, PyPI
`framework-agents`) targets **API parity with the fixed TS semantics** —
every behavior that was a real bug during the TS line (and is
regression-pinned there, see [Pinned semantics](pinned-semantics.md)) is
encoded in the port from day one. The wire contracts (A2A over HTTP/SSE) are
identical in both directions, and a cross-language conformance suite makes
that claim executable.

## Port matrix

| TS module | Python module | State |
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

The batch-2 middleware (`model_fallback`, `summarization`,
`context_editing` + `ClearToolUsesEdit`, `llm_tool_selector`,
`tool_emulator`, `filesystem`, `create_sub_agent`, `pii` tool-results) is
ported with deliberate deviations, pinned in `middleware.py`'s docstring and
listed under [Middleware deviations](#middleware-deviations) below.

## Conformance: the wire is shared

Cross-language conformance is a promise made executable by tests:

- **The Python A2A client speaks the exact wire contract of the TypeScript
  A2A server, and vice versa** — JSON-RPC 2.0 envelope
  (`{jsonrpc, id, method: "message/stream" | "message/send", params:
  {message: {parts}, contextId?}}`), SSE frames
  (`{"contextId", "event"}` / `{"contextId", "done", "text"}` /
  `{"contextId", "error"}`), the bare `[DONE]` terminator, and the
  server-assigned `contextId` chain that makes multi-turn continuation work.
- **`[DONE]` travels in both wire forms.** The servers terminate SSE streams
  with a bare `data: [DONE]` frame; the Python client treats `data: [DONE]`
  and `data: "[DONE]"` (the JSON-stringified variant written by the TS
  server's writer) identically, so either quoting form ends a stream.
- **`tests/test_servers_and_clients.py`** (in `packages/python/tests/`) runs
  both server and client transports end-to-end offline, including the
  Python-side A2A server implementation of the same contract.
- **`tests/test_cross_language.py`** is the strongest pin: it boots a *real*
  TypeScript A2A server (stub model — no network calls to a provider) via
  `tsx` from `packages/typescript`, then drives it from Python with
  `A2AClient` and `AgentClient`: fetches the agent card, round-trips
  `message/send` with contextId continuity, and streams to `done` while
  checking the contextId arrives in the SSE frames. The fixture skips
  automatically when the TS toolchain is absent (or `CROSS_LANG=0` is set)
  so plain-Python environments stay green. The envelope bug — a client
  POSTing without the JSON-RPC `params` and the server silently running a
  stream over an *empty* message — is exactly what this test exists to catch.

## Documented deviations

The deviations below are deliberate, port-level, and pinned in module
docstrings. Everything not listed here matches TS behavior by contract.

### Streaming: done-event vs generator return

TypeScript's `agent.stream()` is a generator whose final `AgentResult` rides
the `done` sentinel. Python async generators cannot carry return values, so
`Agent.stream()` yields a `done` event and the agent records its result
immediately *before* yielding it; `AgentStream` (what `stream()` returns)
captures that result **per stream** at the moment `done` is observed, so
concurrent streams on one agent never cross-contaminate results. `run()`
drains the stream internally — the wire contracts are identical.

### Collector semantics

Related to the above: `collect_stream()` buckets events and exposes the
run's result, but a Python stream that ends without capturing a result
raises a non-retryable `RUN_INCOMPLETE` instead of guessing — a partial or
aborted consumption never fabricates an `AgentResult`. TS `collectStream`
returns `{ events, result }` off the generator's return value; the Python
collector returns the same shape off `AgentStream.result`.

### Sandbox: thread abandonment

The TS sandbox can hard-interrupt code mid-statement (Node's `vm` module).
The Python sandbox executes sandboxed code in a **daemon worker thread**
enforced with `asyncio.wait_for`: the timeout surfaces per contract, but
the abandoned thread cannot be hard-killed and lingers until the process
exits (being a daemon, it cannot keep the process alive). The tool output
and error semantics are unchanged on the outside.

### Lazy serve init

TS `serve` builds one agent per definition while serving starts. The Python
port builds only the agent card at boot and runs `init_chat_model` lazily
inside the A2A contextId-scoped agent factory — never at boot — so a single
missing API key cannot take the whole served ladder down.

### Env precedence

TS serve precedence: process environment < framework `envFile` < `env {}` <
per-agent `envFile` (per-agent files load with `override: true` and *can*
clobber real environment variables). The Python port applies **setdefault
semantics everywhere**: a real environment variable is never clobbered, and
between two env files the *first loaded* wins.

### Default env-file precedence

Outside `serve`, both implementations walk up from the cwd looking for a
`.env` (the process environment always wins; in Python a `FRAMEWORK_ENV_PATH`
override short-circuits the walk). The Python `.env` parser also fixes
classic dotenv divergences: `export KEY=v` prefixes are supported, unquoted
trailing comments (`KEY=v # note`) are stripped, and duplicate keys within
one file resolve last-wins — all parity-checked against the TS loader.

### [DONE] framing acceptance

See [Conformance](#conformance-the-wire-is-shared) above: the Python SSE
clients accept both the bare `[DONE]` frame and the JSON-stringified
`"[DONE]"` form, so streams terminate regardless of which writer produced
them.

### Filesystem / async IO

Filesystem middleware tools run file IO through `asyncio.to_thread`; the
storage classes (sessions, checkpointer, file memory) use blocking IO
inside async methods — acceptable because they are built for
single-process locality, not concurrent multiplexing. Behavior and wire
shapes are unchanged.

### Middleware deviations

Pinned in `middleware.py`'s docstring:

- `model_fallback` replays the **same** request to each fallback in order
  and re-raises the last error when all providers fail.
- `summarization` defaults the summary prefix to `"Prior context summary"`
  (TS: `"Summary of earlier context:"`); the summary call is one user
  message with the transcript embedded, matching the TS shape otherwise.
- `ClearToolUsesEdit` strips old tool-call/tool-result **pairs** (matched by
  `tool_call_id`); the TS version only replaces old tool *outputs* with a
  placeholder. Pinned messages survive either way — pinning one half of a
  pair keeps the whole pair.
- `llm_tool_selector` degrades to the original unfiltered tool list when the
  selector model throws or picks nothing.
- `tool_emulator` records tool descriptions from `before_model`, so the
  emulation prompt can carry them (TS sends name + args only).
- `filesystem` tools return **error feedback strings** on escapes / edit
  misses instead of raising (`FS_ESCAPE` / `FS_NO_MATCH` in TS), so the run
  continues; paths are validated with `pathlib` resolve + `relative_to`.
- `create_sub_agent` returns model-visible feedback for unknown subagents
  (TS raises `SUBAGENT_UNKNOWN`).
- The old fixed-backoff `toolRetry` is folded into `tool_retry`.
- Retry verdicts: a call is retried only when `get_retryable` says `True`;
  every fatal `AgentError` (budgets, tool-call limits) always passes
  through untouched, and unknown verdicts are not auto-retried.

### Serve: session-manager wiring is reserved

`SessionManager` support in `AgentOptions` has not been wired into the
Python agent loop yet; the per-context manager is attached to the built
`Agent` as a `session_manager` attribute (sessionId `"{name}:{contextId}"`)
so the wiring is reserved and ready.

### Semantic defaults

Version graphs default to `urn:framework:ontology:<version>` in Python (the
TS default prefix embeds a personal name); both are configurable, and no
personal or brand names appear in the Python package.

## Running the suites

**TypeScript** (from the monorepo root):

```bash
pnpm install
pnpm --filter framework-agents build
pnpm --filter framework-agents test        # vitest run
pnpm --filter framework-agents lint        # eslint
pnpm --filter framework-agents typecheck   # tsc --noEmit
```

Live provider tests are gated — they hit the real Together.ai API only when
the key and the gate are present:

```bash
E2E_MODEL=1 pnpm --filter framework-agents test -- --run tests/e2e/together.live.test.ts
```

**Python** (from `packages/python`):

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest tests -q                 # offline parity suite
```

The offline suite contains more than 200 parity tests and runs entirely
offline. Live suites are gated by marker:

```bash
E2E_MODEL=1 .venv/bin/python -m pytest tests -q -m live
```

The live Together parity runs immediately; the OpenAI and Anthropic live
suites skip cleanly until `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` appear in
the root `framework/.env`.

**Cross-language conformance** needs no extra command — it is part of the
Python offline suite (`tests/test_cross_language.py`). The fixture boots
the TypeScript server itself (requires the TS package and `tsx` via
`npx --no-install`); when they are absent the tests skip with a clear
reason, and `CROSS_LANG=0` forces the skip. `tests/test_servers_and_clients.py`
covers the Python-side server against the same wire contract.