# Middleware & HITL

Middleware controls the agent loop without forking it. A middleware is an object with a `name`, optional contributed `tools`, and any subset of six hooks; wire it with `middleware:` on `AgentOptions` (a list in both languages).

## Hook contract (pinned order)

| Hook | Runs | Order |
|---|---|---|
| `beforeModel` | before the model call; may replace messages/tools/format | **registration order** |
| `wrapModel` | wraps the model call itself | nested, **first registered = outermost** |
| `afterModel` | after the call; may rewrite the completion | **reverse order** |
| `beforeTool` | before a tool executes; may edit args/name or reject | registration order |
| `aroundTool` | wraps raw tool execution | nested, first registered = outermost |
| `afterTool` | after execution (and after rejections) | reverse order |

Both languages compose identically (Python's helpers are `run_before_model`, `compose_model_call`, etc.). Python hooks may be sync or async (awaitables are unwrapped). Every hook gets `HookContext { step, invocationState }` — the per-run dict, shared **by reference** across steps and across suspend/resume, never sent to the model.

**Scope guard:** `beforeModel` message *replacements* feed the model call only — the loop's message list stays authoritative for appends and the end-of-run save.

```ts
const lateToolsOff = createMiddleware({          // create_middleware in Python
  name: "no-tools-after-step-2",
  beforeModel: (req, ctx) => ctx.step > 2 ? { ...req, tools: [] } : undefined,
});
// TypeScript:
new Agent({ ..., middleware: [modelRetryMiddleware(), lateToolsOff] });
// Python:
Agent(AgentOptions(..., middleware=[model_retry(), late_tools_off]))
```

## Verdicts

`stampRetryable(err, true|false)` / `stamp_retryable` marks any Error retryable or fatal without touching its shape; `getRetryable(err)` reads the verdict (`AgentError`s map to their flag). Retry middleware honors these: a `false` verdict (budget caps, call limits, HITL) always passes through untouched.

## Prebuilt middlewares

| Purpose (one line) | TypeScript | Python |
|---|---|---|
| Retry model calls: exponential backoff + jitter, error-class filtering, `onFailure: error/continue/fn` | `modelRetryMiddleware(opts)` | `model_retry(max_retries, initial_delay_ms, backoff_factor, max_delay_ms, jitter, retry_on, on_failure)` |
| Retry tool executions with the same semantics (optionally scoped to `tools`) | `toolRetryMiddleware(opts)` / `toolRetry({attempts, backoffMs})` | `tool_retry(..., tools=None)` |
| Fall back across providers when the primary fails (first success wins; last error re-raised) | `modelFallbackMiddleware(...fallbacks)` | `model_fallback(*fallbacks)` |
| Cap model calls per run (uses `ctx.step`; breach → `BUDGET_EXCEEDED`) | `modelCallLimit(maxCalls)` | `model_call_limit(max_calls)` |
| Per-run tool-call budget, global or per-tool; blocked calls → `ToolRejection` feedback (`continue`) or `BUDGET_EXCEEDED` (`error`) | `toolCallLimitMiddleware({ toolName?, runLimit, exitBehavior })` | `tool_call_limit(tool_name=None, run_limit=..., exit_behavior="error")` |
| HITL approval: pause tools for approve/edit/reject/suspend (wildcard semantics below) | `humanInTheLoopMiddleware({ interruptOn, decide? })` | `human_in_the_loop(interrupt_on, decide=None)` |
| PII detection: redact/mask/hash/block across input, output, tool results | `piiMiddleware(piiType, opts)` | `pii_redact(pii_type, strategy="redact", detector=None, ...)` |
| Auto-summarize history past a token trigger, keep the newest N | `summarizationMiddleware({ model, triggerTokens, keepMessages?, ... })` | `summarization_middleware(model, trigger_tokens, keep_messages=20, ...)` |
| Context editing: run history edits (clear old tool uses) before each call | `contextEditingMiddleware({ edits? })` + `ClearToolUsesEdit` | `context_editing_middleware(edits=None)` + `ClearToolUsesEdit` |
| Allow/deny tool routing by exact name or regex (deny wins; missing allow = deny) | — | `policy_filter(allow=None, deny=None, exit_behavior="continue")` |
| Task planning: contributes `write_todos`, tracks per-run state (`listTodos()`) | `todoListMiddleware()` | `todo_list()` (`list_todos()`) |
| Narrow 10+ tools to the relevant few with a selector-model call | `llmToolSelectorMiddleware({ model, maxTools?, alwaysInclude? })` | `llm_tool_selector_middleware(model, max_tools=None, always_include=None)` |
| Emulate tool execution with an LLM (prototyping without side effects) | `toolEmulatorMiddleware({ tools?, model? })` | `tool_emulator_middleware(model, tools=None)` |
| `ls/read_file/write_file/edit_file` rooted at a workspace dir | `filesystemMiddleware({ root })` / `filesystemTools(root)` | `filesystem_middleware(root)` / `filesystem_tools(root)` |
| Named subagents via a `task` tool (isolated context per specialist) | `createSubAgentMiddleware({ model, subagents })` | `create_sub_agent_middleware(model, subagents)` |

Notable per-port details (pinned in the module docstrings):

- **Agent wiring**: contributed `tools` are prepended to the agent's tool list at construction and deduped by name (first wins).
- **Python-only `policy_filter`**: exact-string rules or compiled regexes; patterns are recompiled/sanitized so a stateful pattern can never alternate deny/allow verdicts. `"error"` raises `POLICY_DENIED`, `"continue"` returns feedback the model can act on.
- **PII**: built-in detectors `email`, `credit_card`, `ip`. Strategies: `redact` → `[REDACTED_<TYPE>]`; `mask`; `hash`; `block` (TS raises `PII_BLOCKED`; Python raises a `ToolRejection`). Python's mask keeps the first char + dots, hash is sha256 hexdigest[:16].
- **`ClearToolUsesEdit`**: TS replaces old tool **outputs** with a placeholder (`triggerTokens = 100000`, `keep = 3`); Python strips old tool-call/result **pairs** matched by `tool_call_id`, protecting pinned messages and the newest `keep_recent` pairs — a pinned deviation, documented in `middleware.py`.
- **Filesystem escapes**: TS raises `AgentError("FS_ESCAPE")`; Python returns an error-feedback string so the run continues (pinned deviation).
- **Control errors**: in Python, `AgentError`/`HitlInterrupt` always pass through retry middleware untouched, whatever `on_failure` says — degrading them to feedback would break HITL resume and budget verdicts.

## Human in the loop

```ts
const hitl = humanInTheLoopMiddleware({
  interruptOn: {
    publish: true,                                   // approve | edit | reject
    spend: { allowedDecisions: ["approve", "reject"] },
  },
  decide: async (req) => "approve", // or omit → { suspend: true }
});
```

Decisions: `"approve"` (run), `{ edit: args }` (rewritten args), `{ reject: "reason" }` (`ToolRejection` with the message, or a default "do not retry" text), `{ suspend: true }` (throws `HitlInterrupt` for checkpoint/resume — see [memory & sessions](memory-sessions.md)).

**Wildcard semantics:** stored decisions (resume or `invocationState.__hitl_decisions`) are read by exact `hitlKey(tool, args)` first, then by **bare tool name** — models re-issue interrupted calls with regenerated argument text, so exact keys never converge. A decision not permitted by `allowedDecisions` throws `HITL_DECISION`.

`HitlInterrupt` carries `interruptId` (= `hitlKey(tool, args)` in both languages), `.tool`, and args. Python's `hitl_key` is pinned as `f"{tool}:{json.dumps(args, sort_keys=False, default=str)}"`; beware the Python `except X as e` binding-deletion gotcha when stashing interrupts.

## Related

- [Memory & sessions](memory-sessions.md) — checkpoint/resume built on these interrupts
- [Streaming & events](streaming.md) — how `wrapModel` selects the batched delta fallback and interacts with retry verdicts
- [Budgets](budgets.md) — verdict-driven retry pass-through