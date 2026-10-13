"""Middleware port tests. Parity pins from the TS suite (post-sweep semantics)."""

from __future__ import annotations

import hashlib
import json
import re

import pytest
from pydantic import BaseModel

from framework_agents import (
    Agent,
    AgentError,
    AgentOptions,
    ModelCompletion,
    ScriptedModelProvider,
    ToolCallRequest,
    define_tool,
    stamp_retryable,
)
from framework_agents.middleware import (
    HookContext,
    HitlInterrupt,
    Middleware,
    ModelRequest,
    ToolCallRef,
    ToolRejection,
    create_middleware,
    hitl_key,
    human_in_the_loop,
    model_call_limit,
    model_retry,
    pii_redact,
    policy_filter,
    todo_list,
    tool_call_limit,
    tool_retry,
)
from framework_agents.types import ChatMessage, TokenUsage

CTX = HookContext(step=0)


def tool_call_completion(name: str, args: dict, id: str = "t1") -> ModelCompletion:
    return ModelCompletion(tool_calls=[ToolCallRequest(id=id, name=name, args=args)])


def make_tool(name: str, handler):
    class Args(BaseModel):
        text: str = ""

    async def execute(args, ctx):
        return handler(args)

    return define_tool(name=name, description=f"test tool {name}", schema=Args, execute=execute)


ECHO = make_tool("echo", lambda args: f"echo:{args.text}")


# ---------------------------------------------------------------------------
# 1. Hook ordering: before forward, after REVERSED, wrap nested
#    first-registered-outermost (TS reduceRight).
# ---------------------------------------------------------------------------


class Tracer:
    def __init__(self, tag: str, log: list[str]) -> None:
        self.name = f"trace-{tag}"
        self._tag = tag
        self._log = log

    async def before_model(self, req, ctx):
        self._log.append(f"{self._tag}:before-model")

    async def wrap_model(self, req, next_fn, ctx):
        self._log.append(f"{self._tag}:wrap-in")
        res = await next_fn(req)
        self._log.append(f"{self._tag}:wrap-out")
        return res

    async def after_model(self, res, req, ctx):
        self._log.append(f"{self._tag}:after-model")

    async def before_tool(self, call, ctx):
        self._log.append(f"{self._tag}:before-tool")

    async def around_tool(self, call, next_fn, ctx):
        self._log.append(f"{self._tag}:around-in")
        out = await next_fn()
        self._log.append(f"{self._tag}:around-out")
        return out

    async def after_tool(self, call, output, ctx):
        self._log.append(f"{self._tag}:after-tool")
        return output


async def test_hook_ordering():
    log: list[str] = []
    mws = [Tracer(t, log) for t in "ABC"]
    model = ScriptedModelProvider(
        [tool_call_completion("echo", {"text": "hi"}), ModelCompletion(text="done")]
    )
    agent = Agent(AgentOptions(model=model, tools=[ECHO], middleware=mws))
    result = await agent.run("go")
    assert result.text == "done"
    assert log == [
        # step 0: model hooks
        "A:before-model", "B:before-model", "C:before-model",
        "A:wrap-in", "B:wrap-in", "C:wrap-in",
        "C:wrap-out", "B:wrap-out", "A:wrap-out",
        "C:after-model", "B:after-model", "A:after-model",
        # step 0: tool hooks
        "A:before-tool", "B:before-tool", "C:before-tool",
        "A:around-in", "B:around-in", "C:around-in",
        "C:around-out", "B:around-out", "A:around-out",
        "C:after-tool", "B:after-tool", "A:after-tool",
        # step 1: model hooks again
        "A:before-model", "B:before-model", "C:before-model",
        "A:wrap-in", "B:wrap-in", "C:wrap-in",
        "C:wrap-out", "B:wrap-out", "A:wrap-out",
        "C:after-model", "B:after-model", "A:after-model",
    ]


# ---------------------------------------------------------------------------
# 2. Middleware-contributed tools: PREPENDED, dedupe by name, first wins.
# ---------------------------------------------------------------------------


async def test_middleware_tools_prepended_and_dedupe():
    dup_mw1 = make_tool("dup", lambda a: "middleware1-dup")
    dup_mw2 = make_tool("dup", lambda a: "middleware2-dup")
    extra = make_tool("extra", lambda a: "extra")
    dup_agent = make_tool("dup", lambda a: "agent-dup")
    own = make_tool("own", lambda a: "own")
    mws = [
        Middleware("m1", tools=[dup_mw1, extra]),
        Middleware("m2", tools=[dup_mw2]),
    ]
    agent = Agent(AgentOptions(model=ScriptedModelProvider([]), tools=[dup_agent, own], middleware=mws))
    assert [t.name for t in agent._tools] == ["dup", "extra", "own"]
    assert agent._tools[0] is dup_mw1  # first registered middleware wins the name

    model = ScriptedModelProvider([tool_call_completion("dup", {"text": "x"}), ModelCompletion(text="done")])
    agent = Agent(AgentOptions(model=model, tools=[dup_agent, own], middleware=mws))
    result = await agent.run("go")
    assert result.tool_calls[0].output == "middleware1-dup"


# ---------------------------------------------------------------------------
# 3. model_retry: stamp_retryable verdicts + on_failure "continue".
# ---------------------------------------------------------------------------


class FailureProvider:
    id = "fail"
    capabilities = {"structured_output": False, "streaming": False}

    def __init__(self, errors: list[BaseException], final_text: str = "ok") -> None:
        self._errors = list(errors)
        self._final = final_text
        self.calls = 0

    async def complete(self, options):
        self.calls += 1
        if self.calls <= len(self._errors):
            raise self._errors[self.calls - 1]
        return ModelCompletion(text=self._final, usage=TokenUsage(input_tokens=1, output_tokens=1))


async def test_model_retry_retries_stamped_retryable():
    model = FailureProvider([stamp_retryable(RuntimeError("flaky 1"), True), stamp_retryable(RuntimeError("flaky 2"), True)])
    agent = Agent(
        AgentOptions(
            model=model,
            middleware=[model_retry(max_retries=3, initial_delay_ms=1, jitter=False)],
        )
    )
    result = await agent.run("go")
    assert result.text == "ok"
    assert model.calls == 3


async def test_model_retry_never_retries_stamped_fatal_agent_error():
    model = FailureProvider([AgentError("TOOL_CALL_LIMIT", "blocked", False)])
    agent = Agent(
        AgentOptions(
            model=model,
            middleware=[model_retry(max_retries=2, initial_delay_ms=1)],
        )
    )
    with pytest.raises(AgentError) as err:
        await agent.run("go")
    assert err.value.code == "TOOL_CALL_LIMIT"  # pass-through, untouched
    assert model.calls == 1


async def test_model_retry_never_retries_stamped_false():
    model = FailureProvider([stamp_retryable(RuntimeError("fatal"), False)])
    agent = Agent(
        AgentOptions(
            model=model,
            middleware=[model_retry(max_retries=2, initial_delay_ms=1, on_failure="continue")],
        )
    )
    result = await agent.run("go")
    assert result.text == "Model call failed after retries: fatal"
    assert model.calls == 1  # no retry on an explicit False verdict


async def test_model_retry_on_failure_continue_synth_shape():
    model = FailureProvider([stamp_retryable(RuntimeError("down"), True)] * 4)
    mw = model_retry(max_retries=2, initial_delay_ms=1, jitter=False, on_failure="continue")
    res = await mw.wrap_model(ModelRequest(messages=[]), model.complete, CTX)
    assert isinstance(res, ModelCompletion)
    assert res.text == "Model call failed after retries: down"
    assert res.tool_calls == []
    assert res.usage.input_tokens == 0 and res.usage.output_tokens == 0


# ---------------------------------------------------------------------------
# 4. tool_retry: verdicts honored; AgentError (retryable=False) pass-through.
# ---------------------------------------------------------------------------


async def test_tool_retry_retries_stamped_retryable():
    attempts = {"n": 0}

    def handler(args):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise stamp_retryable(ValueError(f"flaky {attempts['n']}"), True)
        return "recovered"

    flaky = make_tool("flaky", handler)
    model = ScriptedModelProvider([tool_call_completion("flaky", {"text": "x"}), ModelCompletion(text="done")])
    agent = Agent(
        AgentOptions(model=model, tools=[flaky], middleware=[tool_retry(max_retries=3, initial_delay_ms=1, jitter=False)])
    )
    result = await agent.run("go")
    assert result.text == "done"
    assert attempts["n"] == 3
    assert result.tool_calls[0].output == "recovered"


async def test_tool_retry_fatal_passes_through_unretried():
    attempts = {"n": 0}

    def handler(args):
        attempts["n"] += 1
        raise stamp_retryable(ValueError("fatal"), False)

    flaky = make_tool("flaky", handler)
    model = ScriptedModelProvider([tool_call_completion("flaky", {"text": "x"}), ModelCompletion(text="done")])
    agent = Agent(
        AgentOptions(model=model, tools=[flaky], middleware=[tool_retry(max_retries=3, initial_delay_ms=1, jitter=False)])
    )
    with pytest.raises(AgentError) as err:
        await agent.run("go")
    assert err.value.code == "TOOL_EXECUTION"  # raw tool failure wraps like before
    assert attempts["n"] == 1


async def test_tool_retry_agent_error_pass_through():
    attempts = {"n": 0}

    def handler(args):
        attempts["n"] += 1
        raise AgentError("SCHEMA", "bad", False)

    flaky = make_tool("flaky", handler)
    model = ScriptedModelProvider([tool_call_completion("flaky", {"text": "x"}), ModelCompletion(text="done")])
    agent = Agent(
        AgentOptions(model=model, tools=[flaky], middleware=[tool_retry(max_retries=3, initial_delay_ms=1, jitter=False)])
    )
    with pytest.raises(AgentError) as err:
        await agent.run("go")
    assert err.value.code == "SCHEMA"
    assert attempts["n"] == 1


async def test_tool_retry_on_failure_continue_feeds_back():
    attempts = {"n": 0}

    def handler(args):
        attempts["n"] += 1
        raise stamp_retryable(ValueError("gone"), False)

    flaky = make_tool("flaky", handler)
    model = ScriptedModelProvider([tool_call_completion("flaky", {"text": "x"}), ModelCompletion(text="done")])
    agent = Agent(
        AgentOptions(
            model=model,
            tools=[flaky],
            middleware=[tool_retry(max_retries=2, initial_delay_ms=1, jitter=False, on_failure="continue")],
        )
    )
    result = await agent.run("go")
    assert result.text == "done"
    tool_msg = [m for m in result.messages if m.role == "tool"][0]
    assert tool_msg.content == 'Tool "flaky" failed after retries: gone'


# ---------------------------------------------------------------------------
# 5. model_call_limit AgentError[BUDGET_EXCEEDED] at 1 call.
# ---------------------------------------------------------------------------


async def test_model_call_limit_one_call():
    model = ScriptedModelProvider([tool_call_completion("echo", {"text": "hi"}), ModelCompletion(text="done")])
    agent = Agent(AgentOptions(model=model, tools=[ECHO], middleware=[model_call_limit(1)]))
    with pytest.raises(AgentError) as err:
        await agent.run("go")
    assert err.value.code == "BUDGET_EXCEEDED"
    assert err.value.retryable is False
    assert model._n == 1  # never retried, second call never made


# ---------------------------------------------------------------------------
# 6. tool_call_limit: continue -> ToolRejection feedback; error -> AgentError.
# ---------------------------------------------------------------------------


async def test_tool_call_limit_continue_with_feedback():
    model = ScriptedModelProvider(
        [
            ModelCompletion(
                tool_calls=[
                    ToolCallRequest(id="t1", name="echo", args={"text": "a"}),
                    ToolCallRequest(id="t2", name="echo", args={"text": "b"}),
                ]
            ),
            ModelCompletion(text="done"),
        ]
    )
    agent = Agent(
        AgentOptions(
            model=model,
            tools=[ECHO],
            middleware=[tool_call_limit(run_limit=1, exit_behavior="continue")],
        )
    )
    result = await agent.run("go")
    assert result.text == "done"
    # TS parity: a rejected call is still RECORDED in toolCalls, with the
    # feedback as its output (first executed normally, second rejected).
    assert len(result.tool_calls) == 2
    assert result.tool_calls[0].output == "echo:a"
    assert result.tool_calls[1].output == 'Tool "echo" call limit exceeded (1). Do not retry this tool; finish with what you have.'
    feedback = [m for m in result.messages if m.role == "tool" and m.tool_call_id == "t2"][0]
    assert feedback.content == 'Tool "echo" call limit exceeded (1). Do not retry this tool; finish with what you have.'


async def test_tool_call_limit_error_raises():
    model = ScriptedModelProvider(
        [
            ModelCompletion(tool_calls=[ToolCallRequest(id="t1", name="echo", args={"text": "a"})]),
            ModelCompletion(text="done"),
        ]
    )
    agent = Agent(
        AgentOptions(model=model, tools=[ECHO], middleware=[tool_call_limit(run_limit=0, exit_behavior="error")])
    )
    with pytest.raises(AgentError) as err:
        await agent.run("go")
    assert err.value.code == "BUDGET_EXCEEDED"


def test_tool_call_limit_requires_run_limit():
    with pytest.raises(ValueError):
        tool_call_limit()


# ---------------------------------------------------------------------------
# 7. policy_filter: string equality + sanitized regex; "g-flagged" patterns
#    are recompiled so verdicts can never alternate.
# ---------------------------------------------------------------------------


class GFlaggedPattern:
    """Stands in for a JS /g regex: stateful across uses (alternates)."""

    pattern = "admin_.*"
    flags = 0

    def __init__(self) -> None:
        self._toggle = False

    def search(self, text):
        self._toggle = not self._toggle
        return True if self._toggle else None


async def test_policy_deny_g_flagged_regex_does_not_alternate():
    mw = policy_filter(deny=[GFlaggedPattern()])

    async def denied(name: str) -> bool:
        try:
            await mw.before_tool(ToolCallRef(name=name, args={}), CTX)
            return False
        except ToolRejection:
            return True

    verdicts = []
    for i in range(12):
        verdicts.append(await denied("admin_delete" if i % 2 == 0 else "list_files"))
    # Stateful semantics would alternate True/False; sanitized recompile cannot.
    assert verdicts == [True, False] * 6


async def test_policy_exact_strings_and_allow_list():
    deny_mw = policy_filter(deny=["rm"])  # plain string = exact equality
    with pytest.raises(ToolRejection):
        await deny_mw.before_tool(ToolCallRef(name="rm", args={}), CTX)
    assert await deny_mw.before_tool(ToolCallRef(name="remove", args={}), CTX) is None

    allow_mw = policy_filter(allow=["echo"])
    with pytest.raises(ToolRejection):
        await allow_mw.before_tool(ToolCallRef(name="rm", args={}), CTX)
    assert await allow_mw.before_tool(ToolCallRef(name="echo", args={}), CTX) is None


async def test_policy_error_exit_and_compiled_regex():
    mw = policy_filter(deny=[re.compile("admin_.*")], exit_behavior="error")
    with pytest.raises(AgentError) as err:
        await mw.before_tool(ToolCallRef(name="admin_delete", args={}), CTX)
    assert err.value.code == "POLICY_DENIED"
    assert await mw.before_tool(ToolCallRef(name="list_files", args={}), CTX) is None  # recompiled, still consistent


# ---------------------------------------------------------------------------
# 8. todo_list roundtrip.
# ---------------------------------------------------------------------------


async def test_todo_list_roundtrip_and_per_run_reset():
    todos = todo_list()
    model = ScriptedModelProvider(
        [tool_call_completion("write_todos", {"items": ["one", "two", "three"]}), ModelCompletion(text="planned")]
    )
    agent = Agent(AgentOptions(model=model, tools=[ECHO], middleware=[todos]))
    result = await agent.run("plan this")
    assert result.tool_calls[0].name == "write_todos"
    assert todos.list_todos() == [
        {"content": "one", "status": "active"},
        {"content": "two", "status": "pending"},
        {"content": "three", "status": "pending"},
    ]
    tool_message = [m for m in result.messages if m.role == "tool"][0]
    assert "Todos updated (3 open)" in tool_message.content

    await agent.run("again")  # plain-text run resets per-run storage at step 0
    assert todos.list_todos() == []


# ---------------------------------------------------------------------------
# 9. PII redact (email on output) + mask/hash/block/input strategies.
# ---------------------------------------------------------------------------


async def test_pii_redact_email_on_output():
    model = ScriptedModelProvider(
        [ModelCompletion(text="Contact jane.doe@example.com or bob@corp.io now.")]
    )
    agent = Agent(AgentOptions(model=model, middleware=[pii_redact("email")]))
    result = await agent.run("hi")
    assert result.text == "Contact [REDACTED_EMAIL] or [REDACTED_EMAIL] now."
    assert "jane.doe@example.com" not in result.messages[-1].content


async def test_pii_strategies_mask_hash_block_and_input():
    masked = pii_redact("email", strategy="mask")
    res = await masked.after_model(ModelCompletion(text="mail a@b.org now"), ModelRequest(messages=[]), CTX)
    assert res.text == "mail a...... now"  # first char kept, the 6 others become dots

    hashed = pii_redact("email", strategy="hash")
    res = await hashed.after_model(ModelCompletion(text="mail jane.doe@example.com now"), ModelRequest(messages=[]), CTX)
    expected = hashlib.sha256(b"jane.doe@example.com").hexdigest()[:16]
    assert res.text == f"mail {expected} now"

    blocked = pii_redact("email", strategy="block")
    with pytest.raises(ToolRejection):
        await blocked.after_model(ModelCompletion(text="mail j@x.io now"), ModelRequest(messages=[]), CTX)
    assert await blocked.after_model(ModelCompletion(text="clean"), ModelRequest(messages=[]), CTX) is None

    scrubbed_input = pii_redact("credit_card", apply_to_input=True, apply_to_output=False)
    req = await scrubbed_input.before_model(
        ModelRequest(messages=[ChatMessage(role="user", content="pay card 4111111111111111, ok")]), CTX
    )
    assert req.messages[0].content == "pay card [REDACTED_CREDIT_CARD], ok"


# ---------------------------------------------------------------------------
# 10/11. HITL: suspend -> resume with the exact-key approve, and the bare
# tool-name WILDCARD covering a re-issued call with DIFFERENT args.
# ---------------------------------------------------------------------------


class DangerousProvider:
    """Returns a `dangerous` call (args advance per call) until its tool
    result lands in history, then the final answer."""

    id = "danger"
    capabilities = {"structured_output": False, "streaming": False}

    def __init__(self, args_seq: list[dict]) -> None:
        self._args_seq = list(args_seq)
        self._n = 0

    async def complete(self, options):
        self._n += 1
        if any(m.role == "tool" and m.tool_name == "dangerous" for m in options.messages):
            return ModelCompletion(text="all done")
        args = self._args_seq[min(self._n - 1, len(self._args_seq) - 1)]
        return ModelCompletion(tool_calls=[ToolCallRequest(id="c1", name="dangerous", args=args)])


DANGEROUS = make_tool("dangerous", lambda args: f"ran dangerous on {args.text}")


def test_hitl_key_pinned_format():
    assert hitl_key("t", {"b": 1, "a": 2}) == 't:{"b": 1, "a": 2}'  # sort_keys=False pinned
    assert hitl_key("t", None) == f"t:{json.dumps(None, default=str)}"
    mw = create_middleware(Middleware("x"))
    assert isinstance(mw, Middleware)  # identity helper


async def test_hitl_suspend_then_exact_key_resume():
    provider = DangerousProvider([{"text": "prod"}, {"text": "prod"}])
    agent = Agent(
        AgentOptions(model=provider, tools=[DANGEROUS], middleware=[human_in_the_loop({"dangerous": True})])
    )
    state: dict = {}
    with pytest.raises(HitlInterrupt) as err:
        await agent.run("deploy", invocation_state=state)
    assert err.value.tool == "dangerous"
    key = hitl_key("dangerous", {"text": "prod"})
    assert err.value.interrupt_id == key

    state["__hitl_decisions"] = {key: "approve"}  # exact key
    result = await agent.run("deploy", invocation_state=state)
    assert result.text == "all done"
    assert result.tool_calls[0].args.model_dump() == {"text": "prod"}
    assert result.invocation_state is state


async def test_hitl_bare_tool_name_wildcard_covers_reissued_different_args():
    """Pinned regression: models re-issue interrupted calls with regenerated
    args, so exact keys never converge — a decision stored under the bare
    tool name approves the re-issued call with DIFFERENT args."""
    provider = DangerousProvider([{"text": "prod"}, {"text": "prod-2"}])
    state: dict = {}
    agent = Agent(
        AgentOptions(
            model=provider,
            tools=[DANGEROUS],
            middleware=[human_in_the_loop({"dangerous": True})],
            invocation_state=state,  # options-level state (kwarg overrides)
        )
    )
    with pytest.raises(HitlInterrupt):
        await agent.run("go")  # no decision yet -> suspend

    state["__hitl_decisions"] = {"dangerous": "approve"}  # bare tool-name wildcard
    result = await agent.run("go")  # resumed call re-issued with DIFFERENT args
    assert result.tool_calls[0].args.model_dump() == {"text": "prod-2"}
    assert result.text == "all done"


async def test_hitl_reject_edit_and_decide_callback():
    provider = DangerousProvider([{"text": "prod"}])
    state = {"__hitl_decisions": {"dangerous": {"reject": "not on your life"}}}
    agent = Agent(AgentOptions(model=provider, tools=[DANGEROUS], middleware=[human_in_the_loop({"dangerous": True})]))
    result = await agent.run("go", invocation_state=state)
    # TS parity: the rejected call is recorded with output=feedback, never executed.
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "dangerous"
    assert result.tool_calls[0].output == "not on your life"
    feedback = [m for m in result.messages if m.role == "tool"][0]
    assert feedback.content == "not on your life"

    # edit rewrites the args the tool actually receives
    provider = DangerousProvider([{"text": "prod"}])
    state = {"__hitl_decisions": {hitl_key("dangerous", {"text": "prod"}): {"edit": {"text": "staging"}}}}
    agent = Agent(AgentOptions(model=provider, tools=[DANGEROUS], middleware=[human_in_the_loop({"dangerous": True})]))
    result = await agent.run("go", invocation_state=state)
    assert result.tool_calls[0].args.model_dump() == {"text": "staging"}

    # inline decide() may suspend; allowed_decisions narrow what a decision may be
    seen: list[dict] = []

    async def decide(req):
        seen.append(dict(req))
        return {"suspend": True}

    provider = DangerousProvider([{"text": "prod"}])
    agent = Agent(AgentOptions(model=provider, tools=[DANGEROUS], middleware=[human_in_the_loop({"dangerous": True}, decide)]))
    with pytest.raises(HitlInterrupt):
        await agent.run("go")
    assert seen[0]["name"] == "dangerous" and seen[0]["interrupt_id"] == hitl_key("dangerous", {"text": "prod"})

    provider = DangerousProvider([{"text": "prod"}])
    agent = Agent(
        AgentOptions(
            model=provider,
            tools=[DANGEROUS],
            middleware=[human_in_the_loop({"dangerous": {"allowed_decisions": ["reject"]}})],
        )
    )
    state = {"__hitl_decisions": {"dangerous": "approve"}}
    with pytest.raises(AgentError) as err:
        await agent.run("go", invocation_state=state)
    assert err.value.code == "HITL_DECISION"


# ---------------------------------------------------------------------------
# Wiring smoke: agent semantics with NO middleware stay exactly as before.
# ---------------------------------------------------------------------------


async def test_agent_without_middleware_semantics_unchanged():
    events: list[dict] = []
    model = ScriptedModelProvider(
        [
            ModelCompletion(
                tool_calls=[
                    ToolCallRequest(id="t1", name="echo", args={"text": "a"}),
                    ToolCallRequest(id="t2", name="nope", args={}),
                ]
            ),
            ModelCompletion(text="done"),
        ]
    )
    agent = Agent(AgentOptions(model=model, tools=[ECHO]))
    async for ev in agent.stream("go"):
        events.append(ev)
    result = agent._last_result
    assert result.text == "done"
    assert [e["type"] for e in events] == ["step", "tool_call", "tool_result", "step", "done"]
    assert [t.name for t in result.tool_calls] == ["echo"]
    unknown = [m for m in result.messages if m.role == "tool" and m.tool_call_id == "t2"][0]
    assert unknown.content == "Unknown tool: nope"
    assert result.invocation_state is None