"""Middleware — control the agent loop without forking it. Parity: src/middleware/index.ts.

Execution order (pinned, matches TS):
- before_model / before_tool: registration order
- after_model / after_tool: REVERSE registration order
- wrap_model / around_tool: nested, first registered = outermost (TS reduceRight)

Pinned deviations from the TS file (per port plan):
- Verdict-driven retry: a call is retried only when ``get_retryable`` says
  True; False (e.g. every fatal AgentError: budgets, tool-call limits) always
  passes through untouched, unknown verdicts are not auto-retried.
- PII mask keeps the first char + dots, hash is sha256 hexdigest[:16];
  "block" raises ToolRejection on an output match.
- Pinned deviations of the batch-2 ports (the rest of the TS built-ins):
  - model_fallback replays the SAME request to each fallback in order and
    re-raises the LAST error when every provider fails.
  - summarization_middleware defaults the summary prefix to
    "Prior context summary" (TS: "Summary of earlier context:"); the summary
    call is a single user message with the transcript embedded (TS
    summarizeWithModel shape).
  - ClearToolUsesEdit STRIPS old tool-call/tool-result PAIRS (matched by
    tool_call_id); the TS version only replaces old tool OUTPUTS with a
    placeholder. Pinned messages (ChatMessage.pinned) always survive -
    pinning either half of a pair keeps the whole pair.
  - llm_tool_selector_middleware degrades to the ORIGINAL unfiltered tool
    list when the selector model throws or picks nothing.
  - tool_emulator_middleware records tool descriptions from before_model so
    the emulation prompt can carry them (TS sends name + args only).
  - filesystem tools return ERROR FEEDBACK STRINGS for escapes / edit
    misses instead of raising AgentError (TS FS_ESCAPE / FS_NO_MATCH) so the
    run continues; paths are validated with pathlib resolve + relative_to.
  - create_sub_agent_middleware returns model-visible feedback for unknown
    subagents (TS raises AgentError SUBAGENT_UNKNOWN).
  (The "old" fixed-backoff ``toolRetry`` is folded into ``tool_retry``.)
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import random
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Optional, Protocol, Union

from pydantic import BaseModel

from .conversation import history_tokens
from .errors import AgentError, get_retryable
from .models import CompleteOptions, ModelCompletion, ModelProvider, ResponseFormatWire, ToolWire
from .tools import Tool, ToolContext, define_tool
from .types import ChatMessage, ReasoningBlock, TextBlock, TokenUsage, ToolCallBlock, text_of


# ---------------------------------------------------------------------------
# Shapes
# ---------------------------------------------------------------------------


@dataclass
class HookContext:
    """Per-step hook context. ``invocation_state`` is shared by reference
    across steps (and across suspend/resume) and never sent to the model."""

    step: int
    invocation_state: dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelRequest:
    """CompleteOptions-shaped request the model hooks see."""

    messages: list[ChatMessage]
    tools: list[ToolWire] = field(default_factory=list)
    response_format: ResponseFormatWire | None = None


@dataclass
class ToolCallRef:
    name: str
    args: Any = None


ModelHookNext = Callable[[ModelRequest], Awaitable[ModelCompletion]]
ToolHookNext = Callable[[], Awaitable[Any]]


class AgentMiddleware(Protocol):
    """Structural protocol; every hook is optional. Hooks may be sync or
    async (sync returns are unwrapped like TS's union return types)."""

    name: str
    tools: list[Tool]

    def before_model(self, req: ModelRequest, ctx: HookContext) -> Optional[ModelRequest]: ...
    def wrap_model(self, req: ModelRequest, next_fn: ModelHookNext, ctx: HookContext) -> ModelCompletion: ...
    def after_model(self, res: ModelCompletion, req: ModelRequest, ctx: HookContext) -> Optional[ModelCompletion]: ...
    def before_tool(self, call: ToolCallRef, ctx: HookContext) -> Optional[ToolCallRef]: ...
    def around_tool(self, call: ToolCallRef, next_fn: ToolHookNext, ctx: HookContext) -> Any: ...
    def after_tool(self, call: ToolCallRef, output: Any, ctx: HookContext) -> Any: ...


class Middleware:
    """Concrete duck-typed AgentMiddleware for the built-ins (and users who
    prefer composition over subclassing). Unset hooks stay absent."""

    def __init__(
        self,
        name: str,
        *,
        tools: Optional[Iterable[Tool]] = None,
        before_model: Optional[Callable[..., Any]] = None,
        wrap_model: Optional[Callable[..., Any]] = None,
        after_model: Optional[Callable[..., Any]] = None,
        before_tool: Optional[Callable[..., Any]] = None,
        around_tool: Optional[Callable[..., Any]] = None,
        after_tool: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.name = name
        self.tools = list(tools) if tools is not None else []
        if before_model is not None:
            self.before_model = before_model
        if wrap_model is not None:
            self.wrap_model = wrap_model
        if after_model is not None:
            self.after_model = after_model
        if before_tool is not None:
            self.before_tool = before_tool
        if around_tool is not None:
            self.around_tool = around_tool
        if after_tool is not None:
            self.after_tool = after_tool


def create_middleware(m: Any) -> Any:
    """Identity factory — documents intent and locks the type."""
    return m


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


# ---------------------------------------------------------------------------
# Hook chains (shared by the agent loop)
# ---------------------------------------------------------------------------


async def run_before_model(middlewares: list[Any], req: ModelRequest, ctx: HookContext) -> ModelRequest:
    for mw in middlewares:
        hook = getattr(mw, "before_model", None)
        if hook is None:
            continue
        new = await _maybe_await(hook(req, ctx))
        if new is not None:
            req = new
    return req


async def run_after_model(middlewares: list[Any], res: ModelCompletion, req: ModelRequest, ctx: HookContext) -> ModelCompletion:
    for mw in reversed(middlewares):
        hook = getattr(mw, "after_model", None)
        if hook is None:
            continue
        new = await _maybe_await(hook(res, req, ctx))
        if new is not None:
            res = new
    return res


def compose_model_call(
    middlewares: list[Any], base: ModelHookNext, ctx: HookContext
) -> Callable[[ModelRequest], Awaitable[ModelCompletion]]:
    """wrap_model chain, nested first-registered-outermost (TS reduceRight)."""

    def bind(wrap: Callable[..., Any], next_fn: ModelHookNext) -> ModelHookNext:
        async def wrapped(req: ModelRequest) -> ModelCompletion:
            return await _maybe_await(wrap(req, next_fn, ctx))

        return wrapped

    async def call(req: ModelRequest) -> ModelCompletion:
        fn = base
        for mw in reversed(middlewares):
            wrap = getattr(mw, "wrap_model", None)
            if wrap is None:
                continue
            fn = bind(wrap, fn)
        return await fn(req)

    return call


async def run_before_tool(middlewares: list[Any], call: ToolCallRef, ctx: HookContext) -> ToolCallRef:
    for mw in middlewares:
        hook = getattr(mw, "before_tool", None)
        if hook is None:
            continue
        new = await _maybe_await(hook(call, ctx))
        if new is not None:
            call = new
    return call


def compose_tool_call(
    middlewares: list[Any], base: ToolHookNext, call: ToolCallRef, ctx: HookContext
) -> Callable[[], Awaitable[Any]]:
    """around_tool chain, nested first-registered-outermost (TS reduceRight)."""

    def bind(wrap: Callable[..., Any], next_fn: ToolHookNext) -> ToolHookNext:
        async def wrapped() -> Any:
            return await _maybe_await(wrap(call, next_fn, ctx))

        return wrapped

    async def run() -> Any:
        fn = base
        for mw in reversed(middlewares):
            wrap = getattr(mw, "around_tool", None)
            if wrap is None:
                continue
            fn = bind(wrap, fn)
        return await fn()

    return run


async def run_after_tool(middlewares: list[Any], call: ToolCallRef, output: Any, ctx: HookContext) -> Any:
    for mw in reversed(middlewares):
        hook = getattr(mw, "after_tool", None)
        if hook is None:
            continue
        new = await _maybe_await(hook(call, output, ctx))
        if new is not None:
            output = new
    return output


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ToolRejection(Exception):
    """Thrown to skip execution with model-visible feedback (the loop turns
    this into a tool message and continues)."""

    def __init__(self, feedback: str) -> None:
        super().__init__(feedback)
        self.feedback = feedback


class HitlInterrupt(Exception):
    """Thrown to suspend a run for human review. The caller catches it,
    stores a decision in ``invocation_state["__hitl_decisions"]``, and
    resumes (the pinned decision-key semantics live in human_in_the_loop)."""

    def __init__(self, interrupt_id: str, tool: str, args: Any = None) -> None:
        super().__init__(f'Approval needed for tool "{tool}" (interrupt {interrupt_id})')
        self.interrupt_id = interrupt_id
        self.tool = tool
        self.args = args


HITL_DECISIONS_KEY = "__hitl_decisions"


def hitl_key(tool: str, args: Any) -> str:
    """Stable key binding a decision to one tool call.

    Pinned: ``f"{tool}:{json.dumps(args, sort_keys=False, default=str)}"``.
    """
    return f"{tool}:{json.dumps(args, sort_keys=False, default=str)}"


HitlDecision = Union[str, dict[str, Any]]  # "approve" | {"reject": msg} | {"edit": args} | {"suspend": True}


def human_in_the_loop(
    interrupt_on: dict[str, Any],
    decide: Optional[Callable[[dict[str, Any]], Any]] = None,
) -> Any:
    """HITL approval for tools listed in ``interrupt_on``.

    Stored decisions are read from ``ctx.invocation_state["__hitl_decisions"]``
    by EXACT ``hitl_key(tool, args)`` first, then by the BARE TOOL NAME.
    The bare-name wildcard is a pinned semantic for resume: a model re-issuing
    a call regenerates arg text, so exact keys never converge; a decision
    stored under the tool name approves the re-issued call with different args.

    ``interrupt_on`` values: ``True`` (approve/edit/reject allowed) or a dict
    like ``{"allowed_decisions": ["approve", "reject"]}``.
    """
    default_allowed = ["approve", "edit", "reject"]

    async def before_tool(call: ToolCallRef, ctx: HookContext) -> Optional[ToolCallRef]:
        rule = interrupt_on.get(call.name)
        if rule is None:
            return None
        if rule is True:
            allowed = default_allowed
        elif isinstance(rule, dict):
            allowed = list(rule.get("allowed_decisions", default_allowed))
        else:
            allowed = default_allowed
        interrupt_id = hitl_key(call.name, call.args)
        decisions = ctx.invocation_state.get(HITL_DECISIONS_KEY) or {}
        stored = decisions.get(interrupt_id)
        if stored is None:
            stored = decisions.get(call.name)  # bare tool-name wildcard
        if stored is not None:
            decision: Any = stored
        elif decide is not None:
            decision = await _maybe_await(
                decide({"name": call.name, "args": call.args, "allowed": list(allowed), "interrupt_id": interrupt_id})
            )
        else:
            decision = {"suspend": True}
        if isinstance(decision, dict) and decision.get("suspend"):
            raise HitlInterrupt(interrupt_id, call.name, call.args)
        if decision == "approve":
            if "approve" not in allowed:
                raise AgentError("HITL_DECISION", f'"approve" not allowed for {call.name}', False)
            return None
        if isinstance(decision, dict) and "edit" in decision:
            if "edit" not in allowed:
                raise AgentError("HITL_DECISION", f'"edit" not allowed for {call.name}', False)
            return ToolCallRef(name=call.name, args=decision["edit"])
        message = decision.get("reject") if isinstance(decision, dict) else None
        if "reject" not in allowed:
            raise AgentError("HITL_DECISION", f'"reject" not allowed for {call.name}', False)
        raise ToolRejection(str(message) if message else f"User rejected {call.name}. Do not retry this tool call.")

    return Middleware("human-in-the-loop", before_tool=before_tool)


# ---------------------------------------------------------------------------
# Retry (shared backoff + verdict semantics)
# ---------------------------------------------------------------------------


def _backoff_delay_ms(attempt: int, initial_delay_ms: float, backoff_factor: float, max_delay_ms: float, jitter: bool) -> float:
    grown = initial_delay_ms * (backoff_factor**attempt)
    capped = min(grown, max_delay_ms)
    if not jitter:
        return capped
    return capped * (0.75 + random.random() * 0.5)


def _should_retry(err: BaseException, retry_on: Any) -> bool:
    """Retryable verdict comes from ``get_retryable`` — True retries, False
    never does (tool-call limits / budgets pass straight through), and an
    unknown verdict only retries when an explicit ``retry_on`` filter says
    so. An optional ``retry_on`` (callable or exception-class list) further
    restricts retries but can never override an explicit False verdict."""
    verdict = get_retryable(err)
    if verdict is False:
        return False
    if retry_on is None:
        return verdict is True
    if isinstance(retry_on, (list, tuple)):
        return isinstance(err, tuple(retry_on))
    return bool(retry_on(err))


def _sleep(backoff_ms: float) -> Awaitable[None]:
    return asyncio.sleep(backoff_ms / 1000.0) if backoff_ms > 0 else asyncio.sleep(0)


def _err_message(err: BaseException) -> str:
    return str(err) or err.__class__.__name__


RetryOn = Union[Callable[[BaseException], bool], list[type[BaseException]], None]
OnFailure = Union[str, Callable[[BaseException], str]]  # "error" | "continue" | fn


def model_retry(
    max_retries: int = 3,
    initial_delay_ms: float = 1000,
    backoff_factor: float = 2,
    max_delay_ms: float = 60000,
    jitter: bool = True,
    retry_on: RetryOn = None,
    on_failure: OnFailure = "error",
) -> Any:
    """wrapModel middleware retrying model calls with exponential backoff.

    Verdicts: ``get_retryable`` True retries, False never (a tool-call-limit
    or budget AgentError pass-through is NEVER retried), unknown only via an
    explicit ``retry_on``. ``on_failure="error"`` re-raises the last error,
    a callable synthesizes a completion with its text, and ``"continue"``
    synthesizes a thin-text completion so the run ends gracefully.

    ``on_failure`` applies ONLY to genuine unexpected failures: an
    ``AgentError`` (control errors like budget/handoff exhaust their verdicts
    first, then raise) always passes through untouched — it is never degraded
    into feedback text, whatever ``on_failure`` says. A callable's ``None``
    result is coerced to a fallback text (a ``None`` completion text would
    crash the run later), and non-string returns are stringified.
    """
    max_retries = max(0, int(max_retries))

    async def wrap_model(req: ModelRequest, next_fn: ModelHookNext, ctx: HookContext) -> ModelCompletion:
        last: BaseException | None = None
        for attempt in range(max_retries + 1):
            try:
                return await next_fn(req)
            except Exception as err:  # noqa: BLE001 - rethrown / synthesized below
                last = err
                if not _should_retry(err, retry_on) or attempt == max_retries:
                    break
                await _sleep(_backoff_delay_ms(attempt, initial_delay_ms, backoff_factor, max_delay_ms, jitter))
        # Control errors are never degraded to completion text: budget
        # exhaustions, handoffs, and other AgentErrors (plus HITL control
        # interrupts) must reach the caller regardless of on_failure.
        if isinstance(last, (AgentError, HitlInterrupt)):
            raise last
        failure_text = f"Model call failed after retries: {_err_message(last)}"
        if on_failure == "continue":
            return ModelCompletion(text=failure_text, usage=TokenUsage())
        if callable(on_failure):
            produced = on_failure(last)
            if produced is None:  # a None completion text would crash later
                produced = "Model call failed after retries"
            return ModelCompletion(text=produced if isinstance(produced, str) else str(produced), usage=TokenUsage())
        raise last

    return Middleware("model-retry", wrap_model=wrap_model)


def tool_retry(
    max_retries: int = 3,
    initial_delay_ms: float = 1000,
    backoff_factor: float = 2,
    max_delay_ms: float = 60000,
    jitter: bool = True,
    retry_on: RetryOn = None,
    on_failure: OnFailure = "error",
    tools: Optional[list[str]] = None,
) -> Any:
    """aroundTool middleware retrying tool executions with the same backoff /
    verdict / onFailure semantics as model_retry. AgentErrors honor their
    ``retryable`` flag: fatal ones pass through untouched. At most
    ``max_retries`` retries per call.

    ``on_failure`` applies ONLY to genuine unexpected/transient failures.
    ``AgentError`` and ``HitlInterrupt`` ALWAYS pass through untouched,
    whatever ``on_failure`` says: control errors (SWARM_HANDOFF routing,
    HITL suspension, BUDGET_EXCEEDED) must reach the caller — degrading them
    into tool feedback text would break swarm handoffs and HITL resume. A
    callable's ``None`` result is coerced to a fallback string (never a
    None), and non-string returns are stringified.
    """
    max_retries = max(0, int(max_retries))
    only = set(tools) if tools else None

    async def around_tool(call: ToolCallRef, next_fn: ToolHookNext, ctx: HookContext) -> Any:
        if only and call.name not in only:
            return await next_fn()
        last: BaseException | None = None
        for attempt in range(max_retries + 1):
            try:
                return await next_fn()
            except Exception as err:  # noqa: BLE001 - rethrown / synthesized below
                last = err
                if not _should_retry(err, retry_on) or attempt == max_retries:
                    break
                await _sleep(_backoff_delay_ms(attempt, initial_delay_ms, backoff_factor, max_delay_ms, jitter))
        # Control errors are never degraded to feedback text: SWARM_HANDOFF,
        # HitlInterrupt, and every other AgentError reach the caller as-is.
        if isinstance(last, (AgentError, HitlInterrupt)):
            raise last
        if on_failure == "continue":
            # Degrade gracefully: the error becomes tool feedback, the run finishes.
            return f'Tool "{call.name}" failed after retries: {_err_message(last)}'
        if callable(on_failure):
            produced = on_failure(last)
            if produced is None:  # never a None content on the result
                produced = "Tool failed after retries"
            return produced if isinstance(produced, str) else str(produced)
        raise last

    return Middleware("tool-retry", around_tool=around_tool)


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


def model_call_limit(max_calls: int) -> Any:
    """Cap model calls per run; exceeded -> AgentError(BUDGET_EXCEEDED).
    Stateless (uses ctx.step)."""

    async def before_model(req: ModelRequest, ctx: HookContext) -> Optional[ModelRequest]:
        if ctx.step >= max_calls:
            raise AgentError("BUDGET_EXCEEDED", f"Exceeded model call limit ({max_calls})", False)
        return None

    return Middleware("model-call-limit", before_model=before_model)


def tool_call_limit(
    tool_name: Optional[str] = None,
    run_limit: Optional[int] = None,
    exit_behavior: str = "error",
) -> Any:
    """Per-run tool-call budgets (global or per-tool, counted at execution).
    A call past ``run_limit`` is blocked with a ToolRejection (continue) or
    raises AgentError(BUDGET_EXCEEDED) (error)."""
    if run_limit is None:
        raise ValueError("tool_call_limit: run_limit is required")
    counts: dict[str, int] = {}

    async def before_model(req: ModelRequest, ctx: HookContext) -> Optional[ModelRequest]:
        if ctx.step == 0:
            counts.clear()  # counters are per run
        return None

    async def around_tool(call: ToolCallRef, next_fn: ToolHookNext, ctx: HookContext) -> Any:
        if tool_name is not None and call.name != tool_name:
            return await next_fn()
        key = tool_name or "*"
        used = counts.get(key, 0) + 1
        counts[key] = used
        if used > run_limit:
            if exit_behavior == "error":
                raise AgentError("BUDGET_EXCEEDED", f"Tool call limit exceeded for {tool_name or 'all tools'} ({run_limit})", False)
            raise ToolRejection(
                f'Tool "{call.name}" call limit exceeded ({run_limit}). Do not retry this tool; finish with what you have.'
            )
        return await next_fn()

    return Middleware("tool-call-limit", before_model=before_model, around_tool=around_tool)


# ---------------------------------------------------------------------------
# Policy filter
# ---------------------------------------------------------------------------

_SAFE_RE_FLAGS = re.IGNORECASE | re.MULTILINE | re.DOTALL | re.UNICODE | re.ASCII | re.VERBOSE | re.LOCALE


def _sanitize_pattern(entry: Any) -> re.Pattern[str]:
    """Clone a pattern so stateful/\"global-flag\" patterns (JS /g regexes
    are stateful across uses) can never alternate deny/allow verdicts."""
    if isinstance(entry, re.Pattern):
        return re.compile(entry.pattern, entry.flags)
    # Duck-typed pattern-like (the /g trap stands in for these): recompile
    # from its source, keeping only the stateless re flags.
    flags = getattr(entry, "flags", 0)
    return re.compile(str(getattr(entry, "pattern", entry)), int(flags) & _SAFE_RE_FLAGS)


def _compile_rule(entry: Any) -> tuple[str, Any]:
    """A rule is exact-string equality ('exact') or a sanitized regex ('regex')."""
    if isinstance(entry, str):
        return ("exact", entry)
    return ("regex", _sanitize_pattern(entry))


def _rule_matches(name: str, rules: list[tuple[str, Any]]) -> bool:
    for kind, rule in rules:
        if kind == "exact":
            if name == rule:
                return True
        elif rule.search(name) is not None:
            return True
    return False


def policy_filter(
    allow: Optional[list[Any]] = None,
    deny: Optional[list[Any]] = None,
    exit_behavior: str = "continue",
) -> Any:
    """Allow/deny tool routing. Entries are exact tool-name strings or regexes
    (compiled patterns are SANITIZED: recompiled without stateful behavior,
    so a 'g-flagged' pattern cannot alternate deny/allow). Deny wins over
    allow; a non-matching allow list denies. ``exit_behavior="continue"`` is
    a ToolRejection (feedback, run continues); ``"error"`` raises AgentError."""

    allow_rules = [_compile_rule(r) for r in (allow or [])]
    deny_rules = [_compile_rule(r) for r in (deny or [])]

    async def before_tool(call: ToolCallRef, ctx: HookContext) -> Optional[ToolCallRef]:
        denied = _rule_matches(call.name, deny_rules) or (bool(allow_rules) and not _rule_matches(call.name, allow_rules))
        if denied:
            if exit_behavior == "error":
                raise AgentError("POLICY_DENIED", f'Tool "{call.name}" is not allowed by policy', False)
            raise ToolRejection(f'Tool "{call.name}" is blocked by policy. Choose a different tool or finish with what you have.')
        return None

    return Middleware("policy-filter", before_tool=before_tool)


# ---------------------------------------------------------------------------
# Todo list
# ---------------------------------------------------------------------------


class _WriteTodosArgs(BaseModel):
    items: list[str]


def todo_list() -> Any:
    """Task planning: contributes a ``write_todos`` tool and tracks state per
    run. Exposes ``list_todos()`` -> list of {content, status} dicts with
    status pending | active | completed (the first item is the active one)."""
    state: dict[str, list[dict[str, str]]] = {"todos": []}

    async def execute(args: Any, ctx: ToolContext | None) -> Any:
        todos: list[dict[str, str]] = []
        for index, content in enumerate(list(args.items)):
            todos.append({"content": content, "status": "active" if index == 0 else "pending"})
        state["todos"] = todos
        open_count = sum(1 for t in todos if t["status"] != "completed")
        lines = "\n".join(f'- [{"x" if t["status"] == "completed" else " "}] {t["content"]}' for t in todos)
        return f"Todos updated ({open_count} open):\n{lines}" if todos else "Todos cleared."

    async def before_model(req: ModelRequest, ctx: HookContext) -> Optional[ModelRequest]:
        if ctx.step == 0:
            state["todos"] = []  # storage is per run
        return None

    def list_todos() -> list[dict[str, str]]:
        return [dict(t) for t in state["todos"]]

    mw = Middleware(
        "todo-list",
        tools=[
            define_tool(
                name="write_todos",
                description="Track multi-step progress. Set exactly one item active at a time; mark completed as you finish.",
                schema=_WriteTodosArgs,
                execute=execute,
            )
        ],
        before_model=before_model,
    )
    mw.list_todos = list_todos  # type: ignore[attr-defined]
    return mw


# ---------------------------------------------------------------------------
# PII
# ---------------------------------------------------------------------------

PIIMatch = dict[str, Any]  # {"text": str, "start": int, "end": int}

PII_BUILTIN_DETECTORS: dict[str, re.Pattern[str]] = {
    "email": re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),
    "credit_card": re.compile(r"\b(?:\d[ -]?){13,16}\b"),
    "ip": re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),
}

PIIDetector = Union[str, re.Pattern[str], Callable[[str], list[Any]]]
PIIStrategy = str  # "redact" | "mask" | "hash" | "block"


def _nth_find(content: str, needle: str, n: int) -> int:
    """Start of the n-th (0-based) occurrence of needle, or -1."""
    start = content.find(needle)
    for _ in range(n):
        if start < 0:
            return -1
        start = content.find(needle, start + 1)
    return start


def _overlaps(spans: list[tuple[int, int]], start: int, end: int) -> bool:
    """True if [start, end) intersects any already-accepted span."""
    return any(s < end and start < e for s, e in spans)


def _find_matches(detector: PIIDetector, content: str) -> list[PIIMatch]:
    """Matches as {text, start, end} (original order). A callable detector
    returns matched substrings or (start, end) span tuples.

    Duplicate returns are DEDUPED against real occurrences: a detector
    reporting the same substring twice consumes first occurrence #0, then
    occurrence #1 — never two matches projected onto the FIRST occurrence,
    which would double-patch one span and leave the second real occurrence
    un-redacted (a leak). More duplicates than occurrences are dropped, and
    every accepted match sits on a distinct, non-overlapping span so the
    reverse patching in _scrub can never corrupt."""
    out: list[PIIMatch] = []
    if callable(detector):
        consumed: dict[str, int] = {}  # substring -> occurrences already mapped
        taken: list[tuple[int, int]] = []
        for m in detector(content) or []:
            start: int
            end: int
            if isinstance(m, str):
                if not m:
                    continue
                n = consumed.get(m, 0)
                consumed[m] = n + 1
                start = _nth_find(content, m, n)
                if start < 0:
                    continue  # more duplicates than real occurrences
                end = start + len(m)
            elif isinstance(m, (tuple, list)) and len(m) == 2:
                try:
                    start, end = int(m[0]), int(m[1])
                except (TypeError, ValueError):
                    continue
                start = max(0, min(start, len(content)))
                end = max(start, min(end, len(content)))
            else:
                continue
            if end <= start or _overlaps(taken, start, end):
                continue  # span already claimed: never patch it twice
            taken.append((start, end))
            out.append({"start": start, "end": end, "text": content[start:end]})
        return out
    if isinstance(detector, str):
        pattern = re.compile(detector)
    else:
        pattern = _sanitize_pattern(detector)
    for m in pattern.finditer(content):
        if m.group(0):
            out.append({"text": m.group(0), "start": m.start(), "end": m.end()})
    return out


def _scrub(content: str, detector: PIIDetector, strategy: PIIStrategy, pii_type: str) -> str:
    # Belt-and-braces: every replacement must land on a DISTINCT span —
    # reverse patching two matches onto one span corrupts the text and
    # leaves real occurrences un-redacted.
    unique: dict[tuple[int, int], PIIMatch] = {}
    for m in _find_matches(detector, content):
        unique[(m["start"], m["end"])] = m
    matches = list(unique.values())
    if not matches:
        return content
    if strategy == "block":
        raise ToolRejection(f"Blocked {pii_type} in content: PII policy violation. Do not output {pii_type}; remove it and reply again.")
    out = content
    for m in sorted(matches, key=lambda m: m["start"], reverse=True):
        out = out[: m["start"]] + _apply_strategy(m["text"], pii_type, strategy) + out[m["end"] :]
    return out


def _apply_strategy(text: str, pii_type: str, strategy: PIIStrategy) -> str:
    if strategy == "redact":
        return f"[REDACTED_{pii_type.upper()}]"
    if strategy == "mask":
        return text[:1] + "." * max(0, len(text) - 1)  # keeps first char + dots
    if strategy == "hash":
        return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    raise ValueError(f"pii_redact: unknown strategy {strategy!r}")  # pragma: no cover - validated earlier


def _scrub_blocks(content: list[Any], scrub: Callable[[str], str]) -> list[Any]:
    from .types import parse_content_block

    out: list[Any] = []
    for block in content:
        parsed: Any = block
        if isinstance(block, dict) and block.get("type") in ("text", "reasoning"):
            try:
                parsed = parse_content_block(block)
            except Exception:  # pragma: no cover - malformed block: leave as-is
                parsed = block
        if isinstance(parsed, TextBlock):
            out.append(parsed.model_copy(update={"text": scrub(parsed.text)}))
        elif isinstance(parsed, ReasoningBlock):
            out.append(parsed.model_copy(update={"reasoning": scrub(parsed.reasoning)}))
        else:
            out.append(block)
    return out


def pii_redact(
    pii_type: str,
    strategy: PIIStrategy = "redact",
    detector: Optional[PIIDetector] = None,
    apply_to_output: bool = True,
    apply_to_input: bool = False,
    apply_to_tool_results: bool = False,
) -> Any:
    """PII detection with redact/mask/hash/block strategies.

    - redact: ``[REDACTED_<TYPE>]`` (e.g. ``[REDACTED_EMAIL]``)
    - mask: first char kept, remaining chars become dots
    - hash: sha256 hexdigest[:16] of the match
    - block: ToolRejection on an output match (the run stops; the caller
      decides how to surface it)

    Built-in detectors: email, credit_card, ip. Applied to model output
    (after_model) and/or input (before_model), and — with
    ``apply_to_tool_results=True`` — to string tool outputs (after_tool).
    """
    if strategy not in ("redact", "mask", "hash", "block"):
        raise ValueError(f"pii_redact: unknown strategy {strategy!r}")
    resolved: PIIDetector
    if detector is None:
        resolved = PII_BUILTIN_DETECTORS.get(pii_type, lambda _content: [])
    else:
        resolved = detector

    def scrub(text: str) -> str:
        return _scrub(text, resolved, strategy, pii_type)

    async def after_model(res: ModelCompletion, req: ModelRequest, ctx: HookContext) -> Optional[ModelCompletion]:
        if not apply_to_output:
            return None
        scrubbed = scrub(res.text)
        if scrubbed == res.text:
            return None
        if strategy == "block":
            raise ToolRejection(f"Blocked {pii_type} in model output. Do not output {pii_type}; remove it and reply again.")
        return ModelCompletion(
            text=scrubbed,
            thinking=res.thinking,
            tool_calls=res.tool_calls,
            usage=res.usage,
            finish_reason=res.finish_reason,
        )

    async def before_model(req: ModelRequest, ctx: HookContext) -> Optional[ModelRequest]:
        if not apply_to_input:
            return None
        changed = False
        messages: list[ChatMessage] = []
        for m in req.messages:
            if isinstance(m.content, str):
                scrubbed = scrub(m.content)
                changed = changed or scrubbed != m.content
                messages.append(m.model_copy(update={"content": scrubbed}))
            else:
                messages.append(m.model_copy(update={"content": _scrub_blocks(m.content, scrub)}))
        if not changed:
            return None
        return ModelRequest(messages=messages, tools=req.tools, response_format=req.response_format)

    async def after_tool(call: ToolCallRef, output: Any, ctx: HookContext) -> Any:
        if not apply_to_tool_results or not isinstance(output, str):
            return None
        return scrub(output)  # "block" rejects; other strategies scrub the text

    return Middleware(f"pii-{pii_type}", before_model=before_model, after_model=after_model, after_tool=after_tool)

# ---------------------------------------------------------------------------
# Model fallback
# ---------------------------------------------------------------------------


def model_fallback(*fallbacks: ModelProvider) -> Any:
    """Fall back across providers when the primary model fails (first
    success wins). On a primary failure each fallback gets the SAME request
    in order; when every provider fails, the LAST error is re-raised."""

    async def wrap_model(req: ModelRequest, next_fn: ModelHookNext, ctx: HookContext) -> ModelCompletion:
        try:
            return await next_fn(req)
        except Exception as primary_err:  # noqa: BLE001 - retried below / re-raised
            last: BaseException = primary_err
            for fb in fallbacks:
                try:
                    return await fb.complete(
                        CompleteOptions(messages=req.messages, tools=req.tools, response_format=req.response_format)
                    )
                except Exception as err:  # noqa: BLE001 - re-raised when exhausted
                    last = err
            raise last

    return Middleware("model-fallback", wrap_model=wrap_model)


# ---------------------------------------------------------------------------
# Summarization
# ---------------------------------------------------------------------------


async def _summarize_with_model(model: ModelProvider, messages: list[ChatMessage]) -> str:
    transcript = "\n".join(
        f"[{m.role}] {m.content if isinstance(m.content, str) else text_of(m.content)}" for m in messages
    )
    completion = await model.complete(
        CompleteOptions(
            messages=[
                ChatMessage(
                    role="user",
                    content=f"Summarize this conversation history concisely, preserving decisions, names, and open items:\n{transcript}",
                )
            ],
            tools=[],
        )
    )
    return completion.text


def summarization_middleware(
    model: ModelProvider,
    trigger_tokens: int,
    keep_messages: int = 20,
    summary_prefix: str = "Prior context summary",
    summarize: Optional[Callable[[list[ChatMessage]], Any]] = None,
) -> Any:
    """Auto-summarize history when estimated tokens (conversation.history_tokens)
    pass ``trigger_tokens``, keeping the newest ``keep_messages``. The old
    head is summarized by ``model`` (or a custom ``summarize`` callable) and
    replaced with an assistant message prefixed by ``summary_prefix``; the
    caller's message list is never mutated in place. Text-oriented, like the
    TS original."""
    keep = max(1, int(keep_messages))

    async def before_model(req: ModelRequest, ctx: HookContext) -> Optional[ModelRequest]:
        total = history_tokens(req.messages)
        if total < trigger_tokens:
            return None
        head = req.messages[: max(0, len(req.messages) - keep)]
        if not head:
            return None
        if summarize is not None:
            summary = await _maybe_await(summarize(head))
        else:
            summary = await _summarize_with_model(model, head)
        return ModelRequest(
            messages=[
                req.messages[0],
                ChatMessage(role="assistant", content=f"{summary_prefix}\n{summary}"),
                *req.messages[-keep:],
            ],
            tools=req.tools,
            response_format=req.response_format,
        )

    return Middleware("summarization", before_model=before_model)


# ---------------------------------------------------------------------------
# Context editing
# ---------------------------------------------------------------------------


class ClearToolUsesEdit:
    """Strip old tool-call/tool-result PAIRS (matched by ``tool_call_id``)
    from the history; the ``keep_recent`` newest pairs survive.

    Pinned deviation: the TS ClearToolUsesEdit only replaces old tool
    OUTPUTS with a placeholder — this port REMOVES the pair entirely.
    Pinned messages (``ChatMessage.pinned``) always survive: pinning either
    half of a pair protects both. ``older_than_steps`` gates the edit: when
    set, stripping only activates once the run's step counter reaches it.
    """

    def __init__(self, older_than_steps: int | None = None, keep_recent: int = 0) -> None:
        self.older_than_steps = older_than_steps
        self.keep_recent = max(0, int(keep_recent))

    def apply(self, messages: list[ChatMessage], step: int = 0) -> list[ChatMessage]:
        if self.older_than_steps is not None and step < self.older_than_steps:
            return messages
        ordered_ids: list[str] = []
        for m in messages:
            if m.role == "tool" and m.tool_call_id and m.tool_call_id not in ordered_ids:
                ordered_ids.append(m.tool_call_id)
        protected: set[str] = set()
        if self.keep_recent:
            protected.update(ordered_ids[-self.keep_recent :])
        for m in messages:
            if not m.pinned:
                continue
            if m.role == "tool" and m.tool_call_id:
                protected.add(m.tool_call_id)  # pinned result keeps its call
            elif m.role == "assistant" and isinstance(m.content, list):
                try:
                    blocks = m.blocks()
                except Exception:  # pragma: no cover - malformed content: keep as-is
                    continue
                protected.update(b.id for b in blocks if isinstance(b, ToolCallBlock))  # pinned call keeps its result
        out: list[ChatMessage] = []
        changed = False
        for m in messages:
            if m.role == "tool" and m.tool_call_id and m.tool_call_id not in protected:
                changed = True  # strip the result half
                continue
            if m.role == "assistant" and isinstance(m.content, list):
                try:
                    blocks = m.blocks()
                except Exception:  # pragma: no cover - malformed content: keep as-is
                    out.append(m)
                    continue
                kept = [b for b in blocks if not (isinstance(b, ToolCallBlock) and b.id not in protected)]
                if len(kept) == len(blocks):
                    out.append(m)
                    continue
                changed = True
                if kept:  # text-only remnant survives with its call blocks stripped
                    out.append(m.model_copy(update={"content": [b.model_dump() for b in kept]}))
                continue  # call-only message with every call stripped: drop it
            out.append(m)
        return out if changed else messages


Edit = Any  # duck-typed: apply(messages[, step]) -> list[ChatMessage] (may be awaitable)


def _apply_edit(edit: Edit, messages: list[ChatMessage], step: int) -> Any:
    try:
        arity = len(inspect.signature(edit.apply).parameters)
    except (TypeError, ValueError):  # pragma: no cover - exotic callables
        arity = 2
    return edit.apply(messages, step) if arity >= 2 else edit.apply(messages)


def context_editing_middleware(edits: Optional[list[Edit]] = None) -> Any:
    """beforeModel middleware running history edits in order. Each edit maps
    the message list to a (possibly shorter) one; an unchanged list returns
    ``None`` so the request passes through untouched."""

    resolved = edits if edits is not None else [ClearToolUsesEdit()]

    async def before_model(req: ModelRequest, ctx: HookContext) -> Optional[ModelRequest]:
        messages = req.messages
        changed = False
        for edit in resolved:
            new = await _maybe_await(_apply_edit(edit, messages, ctx.step))
            if new is not None and new is not messages:
                messages = new
                changed = True
        if not changed:
            return None
        return ModelRequest(messages=messages, tools=req.tools, response_format=req.response_format)

    return Middleware("context-editing", before_model=before_model)


# ---------------------------------------------------------------------------
# LLM tool selector
# ---------------------------------------------------------------------------


def llm_tool_selector_middleware(
    model: ModelProvider,
    max_tools: Optional[int] = None,
    always_include: Optional[list[str]] = None,
    system_prompt: Optional[str] = None,
) -> Any:
    """Narrow a wide tool list before the main model call: the selector
    model gets a numbered menu of names + descriptions plus the last user
    query and replies with a comma-separated subset. ``always_include`` names
    are forced in; any failure (selector model error, unparsable reply, no
    surviving names) degrades to the ORIGINAL unfiltered tool list."""
    forced = list(always_include or [])

    async def before_model(req: ModelRequest, ctx: HookContext) -> Optional[ModelRequest]:
        if max_tools is not None and len(req.tools) <= max_tools and not forced:
            return None
        if not req.tools:
            return None
        last_user = next((m for m in reversed(req.messages) if m.role == "user"), None)
        menu = "\n".join(f"{i}. {t.name}: {t.description}" for i, t in enumerate(req.tools, start=1))
        try:
            completion = await model.complete(
                CompleteOptions(
                    messages=[
                        ChatMessage(
                            role="system",
                            content=system_prompt
                            or "Pick relevant tools. Reply with ONLY a comma-separated list of tool names.",
                        ),
                        ChatMessage(
                            role="user",
                            content=f"Tools:\n{menu}\n\nQuery: {text_of(last_user.content) if last_user else ''}",
                        ),
                    ],
                    tools=[],
                )
            )
        except Exception:  # noqa: BLE001 - selector failure degrades to the original
            return None
        picked = {name.strip() for name in completion.text.split(",") if name.strip()}
        picked.update(forced)
        tools = [t for t in req.tools if t.name in picked]
        if max_tools is not None:
            tools = tools[:max_tools]
        if not tools:
            return None
        return ModelRequest(messages=req.messages, tools=tools, response_format=req.response_format)

    return Middleware("llm-tool-selector", before_model=before_model)


# ---------------------------------------------------------------------------
# Tool emulator
# ---------------------------------------------------------------------------


def tool_emulator_middleware(model: ModelProvider, tools: Optional[list[str]] = None) -> Any:
    """Emulate tool execution with an LLM (testing/prototyping without side
    effects). Around the named tools — or all of them — the real execute is
    skipped and the model produces a plausible output string. Tool
    descriptions are recorded from before_model so the emulation prompt can
    carry them; unnamed tools pass through to real execution."""
    only = set(tools) if tools else None
    descriptions: dict[str, str] = {}

    def before_model(req: ModelRequest, ctx: HookContext) -> None:
        for t in req.tools:
            descriptions[t.name] = t.description

    async def around_tool(call: ToolCallRef, next_fn: ToolHookNext, ctx: HookContext) -> Any:
        if only is not None and call.name not in only:
            return await next_fn()
        description = descriptions.get(call.name, "")
        prompt = (
            f'Emulate tool "{call.name}"'
            + (f" ({description})" if description else "")
            + f" with arguments {json.dumps(call.args, default=str)}."
            + " Reply with ONLY the plausible tool output."
        )
        completion = await model.complete(CompleteOptions(messages=[ChatMessage(role="user", content=prompt)], tools=[]))
        return completion.text

    return Middleware("tool-emulator", before_model=before_model, around_tool=around_tool)


# ---------------------------------------------------------------------------
# Filesystem tools
# ---------------------------------------------------------------------------


class _LsArgs(BaseModel):
    path: str = "."


class _ReadFileArgs(BaseModel):
    path: str


class _WriteFileArgs(BaseModel):
    path: str
    content: str


class _EditFileArgs(BaseModel):
    path: str
    old_string: str
    new_string: str


def _resolve_in(root: Path, rel: str) -> Optional[Path]:
    """Validate a caller-supplied path INSIDE root (resolve + relative_to);
    escapes and unreadable paths return None."""
    try:
        full = (root / rel).resolve()
        full.relative_to(root)
        return full
    except (OSError, ValueError):
        return None


def filesystem_tools(root: str | Path) -> list[Tool]:
    """ls / read_file / write_file / edit_file scoped to ``root``. Escapes
    and edit misses are ERROR FEEDBACK STRINGS, not raises (the model sees
    the problem and retries); every file op runs via asyncio.to_thread so
    the loop is never blocked."""
    base = Path(root).resolve()

    async def ls(args: Any, ctx: ToolContext | None) -> str:
        full = _resolve_in(base, args.path)
        if full is None:
            return f"Error: path escapes root: {args.path}"
        try:
            entries = await asyncio.to_thread(os.listdir, full)
        except OSError as err:
            return f"Error: cannot list {args.path}: {err}"
        return "\n".join(entries)

    async def read_file(args: Any, ctx: ToolContext | None) -> str:
        full = _resolve_in(base, args.path)
        if full is None:
            return f"Error: path escapes root: {args.path}"
        try:
            return await asyncio.to_thread(full.read_text, "utf-8")
        except UnicodeDecodeError:
            # Decode failures are FEEDBACK (the TOOL contract), never a raised
            # exception — a raw escape would kill the run with TOOL_EXECUTION.
            return f"Error: cannot read {args.path}: not valid UTF-8"
        except (OSError, ValueError) as err:
            return f"Error: cannot read {args.path}: {err}"

    async def write_file(args: Any, ctx: ToolContext | None) -> str:
        full = _resolve_in(base, args.path)
        if full is None:
            return f"Error: path escapes root: {args.path}"

        def _write() -> None:
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(args.content, "utf-8")

        try:
            await asyncio.to_thread(_write)
        except OSError as err:
            return f"Error: cannot write {args.path}: {err}"
        return f"wrote {args.path} ({len(args.content)} chars)"

    async def edit_file(args: Any, ctx: ToolContext | None) -> str:
        full = _resolve_in(base, args.path)
        if full is None:
            return f"Error: path escapes root: {args.path}"

        def _edit() -> bool | None:
            content = full.read_text("utf-8")
            if args.old_string not in content:
                return None
            full.write_text(content.replace(args.old_string, args.new_string, 1), "utf-8")
            return True

        try:
            found = await asyncio.to_thread(_edit)
        except UnicodeDecodeError:
            # The file is binary/latin-1: feedback, never a raised escape.
            return f"Error: cannot edit {args.path}: not valid UTF-8"
        except (OSError, ValueError) as err:
            return f"Error: cannot edit {args.path}: {err}"
        if found is None:
            return f"Error: oldString not found in {args.path}"
        return f"edited {args.path}"

    return [
        define_tool(
            name="ls",
            description="List files in a directory (relative to workspace root).",
            schema=_LsArgs,
            execute=ls,
        ),
        define_tool(name="read_file", description="Read a file's full text.", schema=_ReadFileArgs, execute=read_file),
        define_tool(
            name="write_file",
            description="Write (create or overwrite) a file.",
            schema=_WriteFileArgs,
            execute=write_file,
        ),
        define_tool(
            name="edit_file",
            description="Replace the first occurrence of oldString with newString in a file.",
            schema=_EditFileArgs,
            execute=edit_file,
        ),
    ]


def filesystem_middleware(root: str | Path) -> Any:
    """Filesystem access middleware: contributes ls/read_file/write_file/
    edit_file scoped to ``root``."""
    return Middleware("filesystem", tools=filesystem_tools(root))


# ---------------------------------------------------------------------------
# Subagents (task tool)
# ---------------------------------------------------------------------------


class _TaskArgs(BaseModel):
    subagent: str
    input: str


def create_sub_agent_middleware(model: ModelProvider, subagents: list[dict[str, Any]]) -> Any:
    """Delegate to named specialists through a ``task`` tool — isolates
    context so the supervisor stays clean. Each call builds the subagent's
    Agent FRESH (from the middleware's ``model`` param) and returns its run
    text; unknown names return model-visible feedback."""
    defs = list(subagents)
    names = "; ".join(f"{s.get('name')} ({s.get('description', '')})".strip() for s in defs)

    async def execute(args: Any, ctx: ToolContext | None) -> str:
        from .agent import Agent, AgentOptions  # lazy: agent.py imports this module

        defn = next((s for s in defs if s.get("name") == args.subagent), None)
        if defn is None:
            return f"Error: unknown subagent: {args.subagent}. Available: {names or 'none'}"
        options = AgentOptions(
            model=model,
            name=str(defn.get("name")),
            description=str(defn.get("description", "")),
            tools=list(defn.get("tools") or []),
        )
        system_prompt = defn.get("system_prompt")
        if system_prompt:
            options.system_prompt = str(system_prompt)
        return (await Agent(options).run(args.input)).text

    task = define_tool(
        name="task",
        description=f"Delegate to a specialist subagent. Available: {names}. Returns the subagent's final answer.",
        schema=_TaskArgs,
        execute=execute,
    )
    return Middleware("subagents", tools=[task])
