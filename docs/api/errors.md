# Errors & retry contract (shared)

One contract across the TypeScript and Python packages: **every failure the
framework raises is an `AgentError` carrying a stable `code` and a
`retryable` verdict.** Nothing else about the error changes — class, shape,
and message stay untouched, so `except`/`catch` code keeps working.

## AgentError shape

```ts
class AgentError extends Error {
  readonly code: string;
  readonly retryable: boolean;
  constructor(code: string, message: string, retryable = false);
}
```

```python
class AgentError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False) -> None: ...
    # .code, .retryable attributes
```

- `retryable=False` by default: **the framework only promises a retry when it
  says so.** Transient classes (network, 429/5xx, timeouts) are marked
  retryable at the raise site.
- The verdict is honored by everything that retries: middleware, remote
  SPARQL, A2A/MCP clients' built-in retry loops.

## Stamping a verdict on any error

TS:

```ts
stampRetryable(err, false);                  // mark fatal; shape/type untouched
stampRetryable(err, true);                   // mark retryable
const verdict = getRetryable(err);
// true | false, or undefined when never marked.
// AgentError maps to its own retryable flag.
```

Python:

```python
stamp_retryable(err, False)
stamp_retryable(err, True)
verdict = get_retryable(err)
# explicit stamp > AgentError field > None (unknown)
```

In both languages the mark rides a non-invasive channel (a well-known symbol
in JS, an attribute in Python) that never changes the error's type or JSON
shape. Non-`AgentError` exceptions are "unknown" until stamped.

## Contract when retrying

- **`getRetryable(err) === false` (or `retryable=False` on an `AgentError`)
  is NEVER retried** — not even with `onFailure: "continue"`, and no
  `retryOn` filter can override it. Budgets (`BUDGET_EXCEEDED`,
  `BUDGET_NO_PRICE`), tool-call limits, policy denials and structural
  validation failures are terminal by design.
- Unknown verdicts retry only when a retry mechanism's own filter says so.
- **Control errors always pass through untouched.** Retry middleware in both
  languages re-raise `AgentError` and the HITL interrupt regardless of
  `onFailure`: degrading `SWARM_HANDOFF` (swarm routing!) or a human-suspension
  into a feedback string would break swarm handoffs and HITL resume.
- `HitlInterrupt` / `HitlInterrupt` (TS/Python) is not an `AgentError` but
  rides the same guarantee: pass-through, never retried, never swallowed.

## Error code table

Codes are identical in both languages unless noted. "Raised by" lists the
module(s) that produce the code. Retryable defaults are as constructed at the
raise sites — a few sites raise the same code with a computed verdict
(noted).

### Budgets & limits

| Code | Meaning | Retryable default | Raised by |
|---|---|---|---|
| `BUDGET_EXCEEDED` | `maxTokens` / `maxCostUsd` / `deadlineMs` passed, `maxToolCalls` / model-call / tool-call limit hit | false | agent loop, `model_call_limit`, `tool_call_limit`(/middleware), budgets check |
| `BUDGET_NO_PRICE` | `maxCostUsd` can't be enforced: no price for the model (set `budgets.prices`) | false | agent budgets check |

### Model & provider

| Code | Meaning | Retryable default | Raised by |
|---|---|---|---|
| `MODEL_ERROR` | HTTP/transport failure, truncation with zero usable content, reasoning-split loss. **Site-dependent verdict**: 429/5xx/network -> true; `length` truncation with zero content and thought-past-budget guards are false (raise maxTokens) | true (except total truncation: false) | all model providers |
| `MODEL_CAPABILITY` | Input needs vision the model lacks | false | model capability check |

### Agent loop & tools

| Code | Meaning | Retryable default | Raised by |
|---|---|---|---|
| `ABORTED` | Run aborted via signal | false | runtime guard (`runWithBudget` / `run_with_budget`), `AbortHandle` |
| `TIMEOUT` | Run/step exceeded its deadline | true | runtime guard |
| `UNKNOWN_TOOL` | Tool name not registered | false | `ToolRegistry`, tool lookup |
| `TOOL_ARG_INVALID` | Args failed schema validation | false | `ToolRegistry` (TS also surfaces invalid args in-loop as feedback) |
| `TOOL_EXECUTION` | Tool body threw a non-framework error (wrapped) | false | agent loop tool dispatch |
| `STRUCTURED_INVALID` | Structured output failed schema validation (self-repair may follow) | true | structured parse |
| `STRUCTURED_NO_MATCH` | No valid structured response produced within `maxSteps` / `maxAttempts` | false | agent loop, structured tool |
| `CHECKPOINT_MISSING` | No checkpoint saved for the thread on resume | false | `Agent.resume` |
| `MEMORY_CORRUPT` | Memory file is not valid message JSON (fails loud, never wipes) | false | `FileMemory` |

### Storage & state

| Code | Meaning | Retryable default | Raised by |
|---|---|---|---|
| `STORAGE_KEY_INVALID` | Storage key contains traversal (`..`) or NUL | false | local file storage |
| `THREAD_ID_INVALID` | threadId is not a single safe directory name | false | `FileCheckpointer` |
| `MESSAGE_NOT_FOUND` | editMessage target not in history | false | `Agent.editMessage` |
| `MESSAGE_NOT_EDITABLE` | Message contains tool calls; branch instead | false | `Agent.editMessage` |

### Python-only durability codes

| Code | Meaning | Retryable default | Raised by |
|---|---|---|---|
| `CHECKPOINT_SERIALIZABLE` | Checkpoint contains non-JSON values (datetime/Path/models) — never `default=str` | false | `FileCheckpointer.save` (Python) |
| `CHECKPOINT_WRITE` | Checkpoint file could not be written after retries | true | `FileCheckpointer.save` (Python) |
| `LOCK_ERROR` | Local file lock failure | false | `with_file_lock` (Python) |

### Governance & policy

| Code | Meaning | Retryable default | Raised by |
|---|---|---|---|
| `HITL_DECISION` | A disallowed decision was used for a HITL-gated tool | false | HITL middleware |
| `POLICY_DENY` (TS) / `POLICY_DENIED` (Python) | Policy denies the tool call with `exitBehavior: "error"`. **Naming differs across the trees** — check both spellings when matching codes on the wire | false | policy filter (TS `tracing/`, Python `middleware.py`) |
| `PII_BLOCKED` (TS only) | `block` strategy hit a match in model input/output. Python's `block` raises a `ToolRejection` instead (model-visible feedback) | false | TS `piiMiddleware` |

### Sandboxed execution

| Code | Meaning | Retryable default | Raised by |
|---|---|---|---|
| `SANDBOX_TIMEOUT` | Sandbox/javascript/command exceeded its time budget | true | sandbox tools |
| `SANDBOX_ERROR` | Sandbox failed (interpreter error, unusable cwd/shell) | false | sandbox tools |
| `SANDBOX_OUTPUT_TOO_LARGE` | Command output exceeded the buffer cap; write to a file and read in chunks | false | sandbox `execute` |

### Multi-agent & workflows

| Code | Meaning | Retryable default | Raised by |
|---|---|---|---|
| `ORCHESTRATION_EMPTY` | Plan has no agents | false | orchestration |
| `ORCHESTRATION_STRATEGY` (Python only) | Unknown strategy string (TS exhaustively typed) | false | orchestration (Python) |
| `ROUTER_NO_MATCH` | Router model produced an invalid/unparsable agent pick (or has no model) | false | orchestration router |
| `SUBAGENT_UNKNOWN` (TS only) | `task` tool asked for an unknown subagent. Python returns model-visible feedback instead | false | TS subagent middleware |
| `SWARM_EMPTY` | Swarm constructed with no agents | false | Swarm |
| `SWARM_UNKNOWN_ENTRY` | entryPoint name not in the roster | false | Swarm |
| `SWARM_HANDOFF` | **Control flow, not an error for the caller**: a member called `handoff_to_agent`; carries the `HandoffSignal` (to/message/context) for the swarm to route. Never retried; always passes through | false (pinned) | Swarm handoff tool |
| `SWARM_INCOMPLETE` (Python only) | Python-only guard: stream drained without a result | false | Swarm.run (Python) |
| `RUN_INCOMPLETE` (Python only) | Async-generator streams cannot return values; raised when a stream/resume drained without a result | false | Python run/collect/resume |
| `WORKFLOW_DUPLICATE_ID` | Two nodes share an id | false | workflow topological sort |
| `WORKFLOW_UNKNOWN_DEP` | Node depends on an id that doesn't exist | false | workflow topological sort |
| `WORKFLOW_CYCLE` | Dependency cycle (including self-dependency) | false | workflow topological sort |

### A2A & remote agents

| Code | Meaning | Retryable default | Raised by |
|---|---|---|---|
| `A2A_TRANSPORT` | Network failure or bad HTTP status. 5xx -> retryable; 4xx (incl. 413) -> false | true for network/5xx, site-computed | A2A clients, A2AServer writes |
| `A2A_CARD` | Agent card fetch failed or is invalid (missing name) | 5xx -> true, else false | A2A clients |
| `A2A_ERROR` | The remote agent returned a JSON-RPC / SSE error frame | false | A2A clients, AgentClient.stream |
| `A2A_BODY_TOO_LARGE` | Request body exceeds the server cap (default 10 MiB) | false | A2AServer |

### MCP

| Code | Meaning | Retryable default | Raised by |
|---|---|---|---|
| `MCP_TRANSPORT` | Network failure, HTTP 5xx, spawn/write failure. Non-5xx HTTP and spawn failures are false | true except 4xx/spawn | MCP clients |
| `MCP_PROTOCOL` | Malformed/empty SSE response from the server | true | MCP HTTP client |
| `MCP_ERROR` | JSON-RPC error object returned by the server | false | MCP clients |
| `MCP_TOOL_ERROR` | The MCP tool executed and reported `isError` | false | MCP clients |
| `MCP_NOT_CONNECTED` | Method called before `connect()` | false | MCP clients |

### Filesystem & remote SPARQL

| Code | Meaning | Retryable default | Raised by |
|---|---|---|---|
| `FS_ESCAPE` (TS only) | Path escapes the filesystem tool's root. Python returns model-visible feedback instead | false | TS filesystem tools |
| `FS_NO_MATCH` (TS only) | `edit_file` oldString not found. Python returns feedback | false | TS `edit_file` |
| `REMOTE_SPARQL` | SPARQL request failed: 429/5xx and network errors retry internally (honoring `Retry-After`); non-JSON SELECT results are false | true except 4xx/non-JSON | remote SPARQL adapter |

Both source trees were grepped for this table; the language-only rows above are
the complete set of asymmetries (they match the deliberate deviations on the
[parity page](../reference/parity.md)).

## Retry-middleware contract (recap)

```ts
// TypeScript — RetryMiddlewareOptions retryOn/onFailure; verdict wins over filters
toolRetryMiddleware({ retryOn: [TimeoutError], onFailure: "continue" });
```

```python
# Python — same semantics via middleware.tool_retry / model_retry
tool_retry(retry_on=[TimeoutError], on_failure="continue")
```

1. `get_retryable(err) is False` -> no retry, error re-raised, ALWAYS —
   even when `onFailure: "continue"` / `on_failure="continue"` is set.
2. `HitlInterrupt` and `SWARM_HANDOFF` always pass through untouched: the
   retry layers re-raise them before any `onFailure` handling, so HITL
   suspension and swarm routing keep working under every middleware stack.
3. `onFailure: "continue"` degrades genuine unexpected failures into
   feedback text (tool message / thin completion) so the run can end
   gracefully; a callable synthesizes the text (a `None` return is coerced to a
   non-`None` fallback); `"error"` (default) re-raises.
4. Backoff knobs (`max_retries`/`initial_delay_ms`/`backoff_factor`/
   `max_delay_ms`/`jitter`) are honored in both languages — exponential
   backoff with jitter (Python defaults `max_retries` to 3, TS to 2).