"""The ReAct loop. Parity: src/agent/index.ts (post-sweep semantics).

Pinned behaviors (each was a real bug in TS once; the python port starts
from the FIXED versions):

- Budgets fire after EVERY model call on CUMULATIVE usage; reasoning tokens
  are a subset of output tokens so they are never added on top.
- Memory save at run end is CLEAR-then-SAVE (save() appends).
- Structured 'auto' resolves by provider capability — an unresolved 'auto'
  made every run throw STRUCTURED_NO_MATCH in TS once.
- maxSteps exhaustion stores a synthesized assistant message (history never
  ends on a dangling tool-call tail).
- Unknown tools / invalid args become model-visible feedback; loop continues.
- result.usage.cost_usd set only when a price is known — never guessed.
- Stream parity: token/thinking deltas batch per model call (as in TS), tool
  events yield live, budget errors pass through unretried.
- Per-stream results: AgentStream captures its OWN AgentResult at the done
  event; concurrent streams on one agent never cross-contaminate.
- beforeModel REPLACEMENTS feed the model call only: the loop's message list
  stays authoritative for appends and the end-of-run save (TS parity).
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable, Literal, Optional, Type

from pydantic import BaseModel, ValidationError

from .checkpoint import PendingCall, ThreadCheckpoint
from .errors import AgentError
from .memory import InMemoryMemory, Memory
from .middleware import (
    HITL_DECISIONS_KEY,
    HookContext,
    HitlInterrupt,
    ModelRequest,
    ToolCallRef,
    ToolRejection,
    compose_model_call,
    compose_tool_call,
    run_after_model,
    run_after_tool,
    run_before_model,
    run_before_tool,
)
from .models import (
    CompleteOptions,
    ModelProvider,
    ResponseFormatWire,
    ToolWire,
)
from .pricing import ModelPrice, price_for, usage_cost
from .sessions import SessionManager, Snapshot, SnapshotData, select_snapshot_fields
from .state import AgentState
from .streaming import AgentStream, StreamEvent
from .tools import Tool, ToolContext, ToolResult, define_tool, parameters_schema
from .types import ChatMessage, ExecutedToolCall, TokenUsage

DEFAULT_SYSTEM = "You are a helpful agent. Use tools when needed. Be concise."
DEFAULT_MAX_STEPS = 6

TimelineKind = Literal["run", "edit", "branch", "resume", "snapshot"]


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TokenBudgets:
    max_tokens: Optional[int] = None
    max_cost_usd: Optional[float] = None
    prices: Optional[ModelPrice] = None  # price override for max_cost_usd
    deadline_ms: Optional[float] = None


@dataclass
class ResponseFormat:
    schema: Type[BaseModel]
    name: str = "structured_response"
    strategy: Literal["auto", "provider", "tool"] = "auto"
    max_attempts: int = 2


@dataclass
class AgentEvents:
    on_step: Optional[Callable[[int], None]] = None
    on_token: Optional[Callable[[str], None]] = None
    on_thinking: Optional[Callable[[str], None]] = None
    on_tool_call: Optional[Callable[[str, Any], None]] = None
    on_tool_result: Optional[Callable[[str, Any], None]] = None
    on_done: Optional[Callable[[str], None]] = None


@dataclass
class AgentResult:
    text: str
    messages: list[ChatMessage]
    tool_calls: list[ExecutedToolCall]
    usage: TokenUsage
    structured_response: Optional[Any] = None
    invocation_state: Optional[dict[str, Any]] = None


def provider_strategy(schema: Type[BaseModel], name: str = "structured_response") -> ResponseFormat:
    return ResponseFormat(schema=schema, name=name, strategy="provider")


def tool_strategy(schema: Type[BaseModel], name: str = "structured_response") -> ResponseFormat:
    return ResponseFormat(schema=schema, name=name, strategy="tool")


@dataclass
class AgentOptions:
    model: ModelProvider
    name: str = "agent"
    description: str = ""
    system_prompt: str = DEFAULT_SYSTEM
    tools: list[Tool] = field(default_factory=list)
    memory: Optional[Memory] = None
    max_steps: int = DEFAULT_MAX_STEPS
    max_tool_calls: Optional[int] = None
    budgets: Optional[TokenBudgets] = None
    response_format: Optional[ResponseFormat] = None
    events: Optional[AgentEvents] = None
    initial_messages: list[ChatMessage] = field(default_factory=list)
    # Middleware hooks (see .middleware); contributed tools are merged at
    # construction: PREPENDED, dedupe by name, first wins.
    middleware: list[Any] = field(default_factory=list)
    # Per-run state shared by reference with middleware hooks (never sent to
    # the model); a run invocation_state kwarg overrides this.
    invocation_state: Optional[dict[str, Any]] = None
    # Agent key-value state (never sent to the model): AgentState or a plain
    # dict (validated and converted at construction).
    state: AgentState | dict[str, Any] | None = None
    # Persist state + history across restarts. Restored on first run (BEFORE
    # initial_messages seeding — the session wins over the seed), saved after
    # each run.
    session_manager: Optional[SessionManager] = None
    # Output version: "v0" assistant turns carry plain text; "v1" also stores
    # thinking as reasoning blocks on final turns.
    output_version: Literal["v0", "v1"] = "v0"


class Agent:
    def __init__(self, options: AgentOptions) -> None:
        self.options = options
        # Middleware-contributed tools are PREPENDED to the agent's tool
        # list (dedupe by name, first wins) so the model can plan with them.
        seen: set[str] = set()
        merged: list[Tool] = []
        for tool in [
            t
            for mw in options.middleware
            for t in (getattr(mw, "tools", None) or [])
        ] + list(options.tools):
            if tool.name in seen:
                continue
            seen.add(tool.name)
            merged.append(tool)
        self._tools = merged
        self._history: list[ChatMessage] = []
        self._seeded = False
        self._restored = False
        self._last_result: Optional[AgentResult] = None
        self._agent_state = (
            options.state
            if isinstance(options.state, AgentState)
            else AgentState(options.state if isinstance(options.state, dict) else None)
        )
        self._revisions: list[dict[str, Any]] = []
        self._instance_session_id: Optional[str] = None

    @property
    def name(self) -> str:
        return self.options.name

    @property
    def messages(self) -> list[ChatMessage]:
        return list(self._history)

    @property
    def state(self) -> AgentState:
        """Key-value state (never sent to the model)."""
        return self._agent_state

    @property
    def session_id(self) -> str:
        """Session handle: the manager's id, or a generated per-instance id."""
        if self.options.session_manager is not None:
            return self.options.session_manager.session_id
        if self._instance_session_id is None:
            self._instance_session_id = secrets.token_hex(4)
        return self._instance_session_id

    @property
    def timeline(self) -> list[dict[str, Any]]:
        """Version timeline: runs, edits, branches, resumes (append-only)."""
        return list(self._revisions)

    def _record(self, kind: TimelineKind, label: Optional[str] = None) -> None:
        entry: dict[str, Any] = {"rev": len(self._revisions) + 1, "kind": kind}
        if label:
            entry["label"] = label
        entry["at"] = _iso_now()
        self._revisions.append(entry)

    def with_tools(self, *extra: Tool) -> "Agent":
        """Clone with extra tools.

        TS clone semantics: memory, session, and state are NOT carried over
        (fresh per clone); model, tools, middleware, and formats are shared;
        dedupe happens via the constructor (first occurrence wins).
        """
        rt = self.options
        return Agent(
            AgentOptions(
                model=rt.model,
                name=rt.name,
                description=rt.description,
                system_prompt=rt.system_prompt,
                tools=[*self._tools, *extra],
                max_steps=rt.max_steps,
                max_tool_calls=rt.max_tool_calls,
                budgets=rt.budgets,
                response_format=rt.response_format,
                middleware=list(rt.middleware),
                events=rt.events,
                output_version=rt.output_version,
            )
        )

    async def edit_message(self, id_or_index: str | int, content: Any) -> ChatMessage:
        """Edit a message in the current history (by id or index) and record
        the version in the timeline. History + memory stay in sync."""
        if isinstance(id_or_index, int):
            idx: Optional[int] = id_or_index
        else:
            idx = None
            for i, message in enumerate(self._history):
                if message.id == id_or_index:
                    idx = i
                    break
        if idx is None or idx < 0 or idx >= len(self._history):
            raise AgentError(
                "MESSAGE_NOT_FOUND", f"No message {id_or_index} in history", retryable=False
            )
        message = self._history[idx]
        # The role:"tool" messages that follow reference this turn's
        # tool_call ids; replacing the content would orphan them (providers
        # 400 on unpaired tool results).
        if not isinstance(message.content, str):
            blocks = message.blocks()
            if any(b.model_dump().get("type") == "tool_call" for b in blocks):
                raise AgentError(
                    "MESSAGE_NOT_EDITABLE",
                    f"Message {id_or_index} contains tool calls; branch() from a point before it instead",
                    retryable=False,
                )
        updated = message.model_copy(update={"content": content})
        self._history = [*self._history[:idx], updated, *self._history[idx + 1 :]]
        if self.options.memory is not None:
            await self.options.memory.clear()
            await self.options.memory.save([m for m in self._history if m.role != "system"])
        self._record("edit", f"message {message.id or idx}")
        return updated

    async def branch(self, label: str, overrides: dict[str, Any] | None = None) -> "Agent":
        """Branch the current history into an independent agent.

        Model, tools, middleware, and formats are shared; state is copied;
        memory is fresh (seeded with this history); sessions are NOT carried
        over (to avoid double-persist collisions). Both timelines record the
        branch.
        """
        rt = self.options
        memory = InMemoryMemory()
        await memory.save([m for m in self._history if m.role != "system"])
        options: dict[str, Any] = {
            "model": rt.model,
            "name": rt.name,
            "description": rt.description,
            "system_prompt": rt.system_prompt,
            "tools": list(self._tools),
            "memory": memory,
            "max_steps": rt.max_steps,
            "max_tool_calls": rt.max_tool_calls,
            "budgets": rt.budgets,
            "response_format": rt.response_format,
            "middleware": list(rt.middleware),
            "events": rt.events,
            "state": self.state.to_json(),
            "output_version": rt.output_version,
        }
        options.update(overrides or {})  # overrides win (TS spread position)
        child = Agent(AgentOptions(**options))
        child._history = list(self._history)
        child._revisions = list(self._revisions)
        child._record("branch", label)
        self._record("branch", label)
        return child

    async def pause(self, thread_id: str, checkpointer: Any) -> dict[str, Any]:
        """Pause mid-conversation: persist history + state under thread_id
        without needing an interrupt. Pair with resume()."""
        await checkpointer.save(
            thread_id,
            ThreadCheckpoint(
                thread_id=thread_id,
                saved_at=_iso_now(),
                messages=list(self._history),
                state=self.state.to_json(),
                pending=[],
            ),
        )
        return {"thread_id": thread_id, "messages": len(self._history)}

    def take_snapshot(self, opts: dict[str, Any] | None = None) -> Snapshot:
        """Manual checkpoint: messages + state (+ system prompt on request)."""
        options = dict(opts or {})
        preset = options.get("preset", "session")
        data = select_snapshot_fields(
            SnapshotData(
                messages=list(self._history),
                state=self.state.to_json(),
                system_prompt=self.options.system_prompt,
            ),
            preset=preset,
            include=options.get("include"),
            exclude=options.get("exclude"),
        )
        return Snapshot(
            id=f"snapshot-{format(int(time.time() * 1000), 'x')}",
            created_at=_iso_now(),
            data=data,
            app_data=options.get("appData") or options.get("app_data"),
        )

    async def load_snapshot(self, snapshot: Snapshot) -> None:
        """Restore fields present in the snapshot; absent fields keep current
        values (state merged key-by-key, messages replaced, memory synced)."""
        data = snapshot.data
        if data.state:
            for key, value in data.state.items():
                self.state.set(key, value)
        if data.messages:
            self._history = list(data.messages)
            if self.options.memory is not None:
                await self.options.memory.clear()
                await self.options.memory.save([m for m in data.messages if m.role != "system"])

    async def resume(
        self,
        thread_id: str,
        decisions: Optional[dict[str, Any]] = None,
        checkpointer: Any = None,
        invocation_state: Optional[dict[str, Any]] = None,
    ) -> AgentResult:
        """Resume a suspended run: restore the checkpoint, seed human
        decisions, and continue the loop WITHOUT a new user message."""
        if checkpointer is None:
            raise ValueError("resume() requires a checkpointer")
        cp = await checkpointer.load(thread_id)
        if cp is None:
            raise AgentError(
                "CHECKPOINT_MISSING", f'No checkpoint for thread "{thread_id}"', retryable=False
            )
        cp_state = dict(getattr(cp, "state", None) or {})
        cp_messages = list(getattr(cp, "messages", None) or [])
        if cp_state:
            for key, value in cp_state.items():
                self.state.set(key, value)
        self._history = cp_messages
        if self.options.memory is not None:
            await self.options.memory.clear()
            await self.options.memory.save([m for m in cp_messages if m.role != "system"])
        invocation = dict(invocation_state or {})
        invocation[HITL_DECISIONS_KEY] = decisions or {}
        stream = AgentStream(self, self._continuation(invocation, checkpointer=checkpointer, thread_id=thread_id))
        async for _ in stream:
            pass
        self._record("resume", thread_id)
        result = stream.result
        if result is None:  # pragma: no cover - continuation sets it
            raise AgentError("RUN_INCOMPLETE", "Resume ended without a result", retryable=False)
        return result

    async def run(
        self,
        input_text: str | list[Any],
        invocation_state: Optional[dict[str, Any]] = None,
        checkpointer: Any = None,
        thread_id: Optional[str] = None,
    ) -> AgentResult:
        """Drain the stream (TS run() drains stream(); hooks fire identically)."""
        stream = self.stream(input_text, invocation_state, checkpointer=checkpointer, thread_id=thread_id)
        async for _ in stream:
            pass
        self._record("run", input_text[:60] if isinstance(input_text, str) else "blocks")
        # Per-stream capture (NOT the agent-global _last_result): concurrent
        # runs on one agent must each keep their own result.
        result = stream.result
        if result is None:  # pragma: no cover - drain always sets it
            raise AgentError("RUN_INCOMPLETE", "Stream drained without a result", retryable=False)
        return result

    def stream(
        self,
        input_text: str | list[Any],
        invocation_state: Optional[dict[str, Any]] = None,
        checkpointer: Any = None,
        thread_id: Optional[str] = None,
    ) -> AsyncIterator[StreamEvent]:
        # AgentStream wraps the generator so the finished AgentResult is
        # reachable via .result (collect_stream) — python async generators
        # cannot carry return values like the TS generator does.
        return AgentStream(self, self._stream(input_text, invocation_state, checkpointer, thread_id))

    @staticmethod
    def _checkpoint_pair(
        checkpointer: Any, thread_id: Optional[str]
    ) -> Optional[tuple[Any, str]]:
        if checkpointer is not None and thread_id:
            return (checkpointer, thread_id)
        return None

    async def _stream(
        self,
        input_text: str | list[Any],
        invocation_state: Optional[dict[str, Any]] = None,
        checkpointer: Any = None,
        thread_id: Optional[str] = None,
    ) -> AsyncIterator[StreamEvent]:
        rt = self.options
        # The kwarg wins over AgentOptions.invocation_state; hooks always see
        # a live dict and AgentResult mirrors whichever one was in play.
        state_provided = invocation_state is not None or rt.invocation_state is not None
        invocation_state = (
            invocation_state if invocation_state is not None else rt.invocation_state if rt.invocation_state is not None else {}
        )

        # Session restore wins over initialMessages: a persisted conversation
        # must not be silently replaced by the seed on the first run.
        if rt.session_manager is not None and not self._restored:
            self._restored = True
            latest = await rt.session_manager.load_latest()
            if latest is not None:
                if latest.state:
                    for key, value in latest.state.items():
                        self.state.set(key, value)
                if latest.messages:
                    self._history = list(latest.messages)
                    if rt.memory is not None and not await rt.memory.load():
                        await rt.memory.save([m for m in latest.messages if m.role != "system"])

        if not self._seeded and rt.initial_messages:
            self._seeded = True
            if rt.memory is not None:
                # The session-restore path above ran FIRST: when it already
                # filled memory, the seed stays out (session wins over seed).
                if not await rt.memory.load():
                    await rt.memory.save([m for m in rt.initial_messages if m.role != "system"])
            elif not self._history:
                self._history = list(rt.initial_messages)

        if rt.memory is not None:
            history = list(await rt.memory.load())
        else:
            history = [m for m in self._history if m.role != "system"]

        messages: list[ChatMessage] = [
            ChatMessage(role="system", content=rt.system_prompt),
            *[m for m in history if m.role != "system"],
            ChatMessage(role="user", content=input_text),
        ]
        async for event in self._run_loop(
            messages, invocation_state, state_provided, self._checkpoint_pair(checkpointer, thread_id)
        ):
            yield event

    async def _continuation(
        self,
        invocation_state: dict[str, Any],
        checkpointer: Any = None,
        thread_id: Optional[str] = None,
        state_provided: bool = True,
    ) -> AsyncIterator[StreamEvent]:
        """Continue the loop from restored history (no new user message)."""
        rt = self.options
        if rt.memory is not None:
            history = list(await rt.memory.load())
        else:
            history = [m for m in self._history if m.role != "system"]
        messages: list[ChatMessage] = [
            ChatMessage(role="system", content=rt.system_prompt),
            *[m for m in history if m.role != "system"],
        ]
        async for event in self._run_loop(
            messages, invocation_state, state_provided, self._checkpoint_pair(checkpointer, thread_id)
        ):
            yield event

    async def _run_loop(
        self,
        messages: list[ChatMessage],
        invocation_state: dict[str, Any],
        state_provided: bool,
        checkpoint: Optional[tuple[Any, str]],
    ) -> AsyncIterator[StreamEvent]:
        rt = self.options
        mw = list(rt.middleware)
        usage = TokenUsage()
        started = time.monotonic()
        executed: list[ExecutedToolCall] = []
        structured_response: Any = None
        has_structured = False
        # Tools see the agent state + the LIVE invocation_state (by reference).
        tool_ctx = ToolContext(agent_name=rt.name, state=self._agent_state, invocation_state=invocation_state)

        # 'auto' resolves by capability; provider/tool stay explicit.
        fmt = rt.response_format
        strategy: str | None = None
        if fmt is not None:
            strategy = (
                "provider" if fmt.strategy == "auto" and rt.model.capabilities.get("structured_output")
                else "tool" if fmt.strategy == "auto"
                else fmt.strategy
            )

        tools = list(self._tools)
        if strategy == "tool" and fmt is not None:
            tools = [*tools, self._structured_tool(fmt)]

        text = ""
        try:
            for step in range(rt.max_steps):
                rt.events and rt.events.on_step and rt.events.on_step(step)
                yield {"type": "step", "step": step}

                # Token/thinking deltas batch per model call (TS parity) while the
                # events hooks fire live inside provider.complete.
                pending: list[StreamEvent] = []

                def on_token(delta: str) -> None:
                    if rt.events and rt.events.on_token:
                        rt.events.on_token(delta)
                    pending.append({"type": "token", "delta": delta})

                def on_thinking(delta: str) -> None:
                    if rt.events and rt.events.on_thinking:
                        rt.events.on_thinking(delta)
                    pending.append({"type": "thinking", "delta": delta})

                response_format = None
                if strategy == "provider" and fmt is not None:
                    response_format = ResponseFormatWire(name=fmt.name, schema=fmt.schema.model_json_schema())

                # Model hooks: before (forward order) -> wrap (first-registered
                # outermost) -> after (reverse order). ctx is shared with the
                # tool hooks below; invocation_state travels by reference.
                ctx = HookContext(step=step, invocation_state=invocation_state)

                async def _call_model(req: ModelRequest) -> Any:
                    return await rt.model.complete(
                        CompleteOptions(
                            messages=req.messages,
                            tools=req.tools,
                            response_format=req.response_format,
                            on_token=on_token,
                            on_thinking=on_thinking,
                        )
                    )

                request = ModelRequest(
                    messages=messages,
                    tools=[
                        ToolWire(name=t.name, description=t.description, parameters_schema=parameters_schema(t))
                        for t in tools
                    ],
                    response_format=response_format,
                )
                if mw:
                    request = await run_before_model(mw, request, ctx)
                    completion = await compose_model_call(mw, _call_model, ctx)(request)
                    completion = await run_after_model(mw, completion, request, ctx)
                else:
                    completion = await _call_model(request)
                # A before_model hook may have REPLACED the message list for
                # this call: replacements feed the model call only; history is
                # preserved (TS parity). The loop's `messages` list stays
                # authoritative for appends and the end-of-run save — adopting
                # a shortened replacement here once destroyed long-term memory.
                for event in pending:
                    if event.get("delta"):
                        yield event

                usage = usage + completion.usage
                self._check_budgets(rt.budgets, usage, started, rt.model.id)

                if not completion.tool_calls:
                    text = completion.text
                    if strategy == "provider" and fmt is not None and not has_structured:
                        try:
                            structured_response = fmt.schema.model_validate_json(_as_json(text))
                            has_structured = True
                        except ValidationError as err:
                            if step < rt.max_steps - 1:
                                messages.append(ChatMessage(role="assistant", content=text))
                                messages.append(
                                    ChatMessage(
                                        role="user",
                                        content=f"Your last response was not valid for \"{fmt.name}\": {err}. Reply with corrected JSON only.",
                                    )
                                )
                                continue
                            raise AgentError("STRUCTURED_INVALID", f"Invalid structured output: {err}", retryable=True) from err
                    if rt.output_version == "v1" and completion.thinking:
                        # v1: thinking is ALSO stored as a reasoning block
                        # (v0 keeps assistant turns as plain text).
                        messages.append(
                            ChatMessage(
                                role="assistant",
                                content=[
                                    {"type": "reasoning", "reasoning": completion.thinking},
                                    {"type": "text", "text": text},
                                ],
                            )
                        )
                    else:
                        messages.append(ChatMessage(role="assistant", content=text))
                    break

                messages.append(
                    ChatMessage(
                        role="assistant",
                        content=[
                            {"type": "tool_call", "id": c.id, "name": c.name, "args": c.args}
                            for c in completion.tool_calls
                        ],
                    )
                )
                for call in completion.tool_calls:
                    if rt.max_tool_calls is not None and len(executed) >= rt.max_tool_calls:
                        raise AgentError("BUDGET_EXCEEDED", f"Exceeded maxToolCalls ({rt.max_tool_calls})", retryable=False)
                    tool = next((t for t in tools if t.name == call.name), None)
                    if tool is None:
                        messages.append(
                            ChatMessage(role="tool", content=f"Unknown tool: {call.name}", tool_call_id=call.id, tool_name=call.name)
                        )
                        continue
                    try:
                        args = tool.schema.model_validate(call.args if isinstance(call.args, dict) else {})
                    except ValidationError as err:
                        # Invalid args are repair feedback, not a crash.
                        messages.append(
                            ChatMessage(
                                role="tool",
                                content=f'Invalid args for tool "{tool.name}": {err}',
                                tool_call_id=call.id,
                                tool_name=tool.name,
                            )
                        )
                        continue
                    call_ref = ToolCallRef(name=call.name, args=call.args)
                    rejected: Optional[str] = None
                    if mw:
                        # beforeTool chain (registration order); ToolRejection is
                        # model-visible feedback and the run continues without
                        # executing the call (TS parity: rejections are recorded
                        # in result.toolCalls with output=feedback, afterTool
                        # applies, and to_result's content becomes the tool
                        # message).
                        try:
                            call_ref = await run_before_tool(mw, call_ref, ctx)
                            if call_ref.name != call.name:
                                tool = next((t for t in tools if t.name == call_ref.name), None)
                                if tool is None:
                                    messages.append(
                                        ChatMessage(
                                            role="tool",
                                            content=f"Unknown tool: {call_ref.name}",
                                            tool_call_id=call.id,
                                            tool_name=call_ref.name,
                                        )
                                    )
                                    continue
                            # An edited call is validated against (possibly) the
                            # new tool schema before execution.
                            args = tool.schema.model_validate(call_ref.args if isinstance(call_ref.args, dict) else {})
                        except ValidationError as err:
                            messages.append(
                                ChatMessage(
                                    role="tool",
                                    content=f'Invalid args for tool "{tool.name}": {err}',
                                    tool_call_id=call.id,
                                    tool_name=tool.name,
                                )
                            )
                            continue
                        except ToolRejection as rejection:
                            rejected = rejection.feedback
                    if rejected is None:
                        rt.events and rt.events.on_tool_call and rt.events.on_tool_call(tool.name, args)
                        yield {"type": "tool_call", "name": tool.name, "args": args}
                    if mw:
                        async def _run_tool() -> Any:
                            return await tool.execute(args, tool_ctx)

                        if rejected is not None:
                            output = rejected
                        else:
                            try:
                                # aroundTool chain (first-registered outermost)
                                # runs around the RAW execution.
                                output = await compose_tool_call(mw, _run_tool, call_ref, ctx)()
                            except ToolRejection as rejection:
                                # AroundTool rejections become the tool output
                                # (feedback); the loop continues below.
                                output = rejection.feedback
                            except (AgentError, HitlInterrupt):
                                raise
                            except Exception as err:  # noqa: BLE001 - tool/hook bugs wrap
                                raise AgentError("TOOL_EXECUTION", f'Tool "{tool.name}" failed: {err}', retryable=False) from err
                    elif rejected is not None:
                        output = rejected
                    else:
                        try:
                            output = await tool.execute(args, tool_ctx)
                        except AgentError:
                            raise
                        except Exception as err:  # noqa: BLE001 - tool bugs wrap
                            raise AgentError("TOOL_EXECUTION", f'Tool "{tool.name}" failed: {err}', retryable=False) from err
                    # afterTool (reverse order) may transform the output of BOTH
                    # executed calls and rejections (TS runs it outside the
                    # execution try).
                    if mw:
                        output = await run_after_tool(mw, call_ref, output, ctx)
                    result = await tool.to_result(output)
                    executed.append(ExecutedToolCall(id=call.id, name=tool.name, args=args, output=output))
                    if strategy == "tool" and fmt is not None and tool.name == fmt.name and not has_structured:
                        try:
                            structured_response = fmt.schema.model_validate(output)
                            has_structured = True
                        except ValidationError:
                            pass  # the tool content already carries repair feedback
                    rt.events and rt.events.on_tool_result and rt.events.on_tool_result(tool.name, output)
                    yield {"type": "tool_result", "name": tool.name, "output": output}
                    messages.append(ChatMessage(role="tool", content=result.content, tool_call_id=call.id, tool_name=tool.name))

                if step == rt.max_steps - 1:
                    # Out of steps mid-tool-use: STORE a synthesized reply so the
                    # history never ends on a dangling tool call, and carry the
                    # LAST tool output so the caller has the answer (TS parity:
                    # `Last output: ${JSON.stringify(last.output)}`).
                    if executed:
                        text = (
                            f"Completed {len(executed)} tool call(s). "
                            f"Last output: {json.dumps(executed[-1].output, default=str)}"
                        )
                    else:
                        text = ""
                    messages.append(ChatMessage(role="assistant", content=text))
        except HitlInterrupt as err:
            # Checkpointed HITL: persist messages-up-to-the-interrupting
            # assistant turn + state + the pending call before re-raising
            # (TS agent/index.ts runLoop cut-search semantics).
            if checkpoint is not None:
                await self._save_hitl_checkpoint(checkpoint, messages, err)
            raise


        if fmt is not None and not has_structured:
            raise AgentError(
                "STRUCTURED_NO_MATCH", f'No valid "{fmt.name}" produced within {rt.max_steps} steps', retryable=False
            )

        self._history = messages
        if rt.memory is not None:
            # save() APPENDS: without the clear, every run duplicates history.
            await rt.memory.clear()
            await rt.memory.save([m for m in messages if m.role != "system"])
        if rt.session_manager is not None:
            await rt.session_manager.save_latest(
                SnapshotData(messages=list(messages), state=self.state.to_json(), system_prompt=rt.system_prompt)
            )

        usage.cost_usd = usage_cost(usage, rt.model.id, rt.budgets.prices if rt.budgets else None)
        # The result is recorded BEFORE the done event is yielded so a stream
        # consumer captures its OWN result at the moment it observes done
        # (AgentStream.result) — instance-global _last_result alone would let
        # concurrent streams cross-contaminate or return a stale result.
        self._last_result = AgentResult(
            text=text,
            messages=list(messages),
            tool_calls=executed,
            usage=usage,
            structured_response=structured_response if has_structured else None,
            invocation_state=invocation_state if state_provided else None,
        )
        rt.events and rt.events.on_done and rt.events.on_done(text)
        yield {"type": "done", "text": text}

    @staticmethod
    def _interrupt_args(err: HitlInterrupt) -> Any:
        """Recover the interrupted call's args.

        BaseException's ``args`` setter converts dict values via PySequence_Tuple
        ({"text": "prod"} becomes ('text',)), so ``err.args`` cannot be trusted.
        The framework's HitlInterrupt carries the args inside its
        ``interrupt_id`` (hitl_key = f"{tool}:{json.dumps(args)}"); parse them
        back from there and fall back to err.args for foreign interrupts.
        """
        marker = f"{err.tool}:"
        if err.interrupt_id.startswith(marker):
            tail = err.interrupt_id[len(marker) :]
            try:
                return json.loads(tail)
            except ValueError:
                return err.args
        return err.args

    async def _save_hitl_checkpoint(
        self, checkpoint: tuple[Any, str], messages: list[ChatMessage], err: HitlInterrupt
    ) -> None:
        """TS cut-search: drop the assistant turn holding the interrupted
        tool_call (matched by tool name + JSON args equality) so a resumed
        run re-issues the call from the prior state."""
        checkpointer, thread_id = checkpoint
        args = self._interrupt_args(err)
        pending_args = json.dumps(args if args is not None else {}, default=str)
        cut = len(messages)
        for i in range(len(messages) - 1, -1, -1):
            message = messages[i]
            if message.role != "assistant" or isinstance(message.content, str):
                continue
            matched = False
            for block in message.content:
                if not isinstance(block, dict) or block.get("type") != "tool_call":
                    continue
                if block.get("name") == err.tool and json.dumps(block.get("args") or {}, default=str) == pending_args:
                    matched = True
                    break
            if matched:
                cut = i
                break
        await checkpointer.save(
            thread_id,
            ThreadCheckpoint(
                thread_id=thread_id,
                saved_at=_iso_now(),
                messages=list(messages[:cut]),
                state=self.state.to_json(),
                pending=[PendingCall(interrupt_id=err.interrupt_id, tool=err.tool, args=args)],
            ),
        )

    def _structured_tool(self, fmt: ResponseFormat) -> Tool:
        state = {"attempts": 0}

        async def execute(args: Any, ctx: ToolContext | None) -> Any:
            state["attempts"] += 1
            try:
                return fmt.schema.model_validate(args).model_dump()
            except ValidationError as err:
                if state["attempts"] >= fmt.max_attempts:
                    raise AgentError(
                        "STRUCTURED_NO_MATCH",
                        f'Invalid "{fmt.name}" after {state["attempts"]} attempts',
                        retryable=False,
                    ) from err
                return f"Error: invalid \"{fmt.name}\": {err}. Fix the arguments and call again."

        async def to_result(output: Any) -> ToolResult:
            return ToolResult(content=output if isinstance(output, str) else _as_json(output))

        return define_tool(
            name=fmt.name,
            description=f'Return the final answer as structured "{fmt.name}". Call exactly once with valid arguments.',
            schema=fmt.schema,
            execute=execute,
            to_result=to_result,
        )

    @staticmethod
    def _check_budgets(budgets: Optional[TokenBudgets], usage: TokenUsage, started: float, model_id: str) -> None:
        if budgets is None:
            return
        # reasoning_tokens is a SUBSET of output_tokens: adding it would
        # double-count and fire budgets early.
        total = usage.input_tokens + usage.output_tokens
        if budgets.max_tokens is not None and total > budgets.max_tokens:
            raise AgentError("BUDGET_EXCEEDED", f"Exceeded maxTokens ({budgets.max_tokens}, used {total})", retryable=False)
        if budgets.max_cost_usd is not None:
            price = budgets.prices if budgets.prices is not None else price_for(model_id)
            if price is None:
                raise AgentError(
                    "BUDGET_NO_PRICE",
                    f'Cannot enforce maxCostUsd: no price for model "{model_id}" (set budgets.prices)',
                    retryable=False,
                )
            cost = usage_cost(usage, model_id, price)
            if cost is not None and cost > budgets.max_cost_usd:
                raise AgentError("BUDGET_EXCEEDED", f"Exceeded maxCostUsd (${budgets.max_cost_usd})", retryable=False)
        if budgets.deadline_ms is not None and (time.monotonic() - started) * 1000 > budgets.deadline_ms:
            raise AgentError("BUDGET_EXCEEDED", f"Exceeded deadline ({budgets.deadline_ms}ms)", retryable=False)


def _as_json(value: Any) -> str:
    if isinstance(value, str):
        try:
            json.loads(value)
            return value
        except ValueError:
            return json.dumps(value)
    return json.dumps(value, default=str)
