"""Batch-2 middleware ports: fallback, summarization, context editing,
LLM tool selector, tool emulator, filesystem tools, subagents, PII tool
results."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel

from framework_agents import (
    Agent,
    AgentOptions,
    ModelCompletion,
    ScriptedModelProvider,
    ToolCallRequest,
    ToolWire,
    define_tool,
)
from framework_agents.middleware import (
    ClearToolUsesEdit,
    HookContext,
    ModelRequest,
    ToolCallRef,
    context_editing_middleware,
    create_sub_agent_middleware,
    filesystem_middleware,
    filesystem_tools,
    llm_tool_selector_middleware,
    model_fallback,
    pii_redact,
    summarization_middleware,
    tool_emulator_middleware,
)
from framework_agents.models import CompleteOptions
from framework_agents.tools import ToolContext
from framework_agents.types import ChatMessage, TokenUsage

CTX = HookContext(step=0)


class StubProvider:
    """Canned-text recording stub (tests get exactly the completions listed)."""

    id = "stub"
    capabilities = {"structured_output": False, "streaming": False}

    def __init__(self, texts: list[str] | None = None, error: BaseException | None = None) -> None:
        self._texts = list(texts or [])
        self.error = error
        self.calls: list[CompleteOptions] = []

    async def complete(self, options: CompleteOptions) -> ModelCompletion:
        self.calls.append(options)
        if self.error is not None:
            raise self.error
        text = self._texts.pop(0) if self._texts else ""
        return ModelCompletion(text=text, usage=TokenUsage(input_tokens=1, output_tokens=1))


class FailingProvider:
    id = "failing"
    capabilities = {"structured_output": False, "streaming": False}

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls = 0

    async def complete(self, options: CompleteOptions) -> ModelCompletion:
        self.calls += 1
        raise self.error


# ---------------------------------------------------------------------------
# model_fallback: order, success-on-second, exhaustion, primary-success.
# ---------------------------------------------------------------------------


async def test_model_fallback_tries_in_order_same_request_success_on_second():
    req = ModelRequest(
        messages=[ChatMessage(role="user", content="the same question")],
        tools=[],
    )
    fb1 = FailingProvider(RuntimeError("fb1 down"))
    fb2 = StubProvider(texts=["from-fb2"])
    mw = model_fallback(fb1, fb2)

    async def next_fn(req: ModelRequest) -> ModelCompletion:
        raise RuntimeError("primary down")

    res = await mw.wrap_model(req, next_fn, CTX)
    assert res.text == "from-fb2"
    # both fallbacks saw the SAME request, in order, exactly once
    assert fb1.calls == 1 and len(fb2.calls) == 1
    assert fb2.calls[0].messages[0].content == "the same question"
    assert fb2.calls[0].tools == []


async def test_model_fallback_exhausted_reraises_last():
    req = ModelRequest(messages=[ChatMessage(role="user", content="q")], tools=[])
    fb1 = FailingProvider(RuntimeError("first"))
    fb2 = FailingProvider(RuntimeError("second (last)"))
    mw = model_fallback(fb1, fb2)

    async def next_fn(req: ModelRequest) -> ModelCompletion:
        raise RuntimeError("primary")

    with pytest.raises(RuntimeError) as err:
        await mw.wrap_model(req, next_fn, CTX)
    assert "second (last)" in str(err.value)  # LAST fallback error, not the primary
    assert fb1.calls == 1 and fb2.calls == 1


async def test_model_fallback_primary_success_skips_fallbacks():
    fb = StubProvider(texts=["never-used"])
    mw = model_fallback(fb)

    async def next_fn(req: ModelRequest) -> ModelCompletion:
        return ModelCompletion(text="primary-ok")

    res = await mw.wrap_model(ModelRequest(messages=[]), next_fn, CTX)
    assert res.text == "primary-ok"
    assert fb.calls == []  # no fallback ever called


async def test_model_fallback_via_agent_run():
    agent = Agent(AgentOptions(model=FailingProvider(RuntimeError("outage")), middleware=[model_fallback(ScriptedModelProvider([ModelCompletion(text="fallback-ok")]))]))
    result = await agent.run("q")
    assert result.text == "fallback-ok"


# ---------------------------------------------------------------------------
# summarization: fires above trigger, head -> summary + kept tail.
# ---------------------------------------------------------------------------


def long_messages() -> list[ChatMessage]:
    return [
        ChatMessage(role="system", content="sys"),
        ChatMessage(role="user", content="one two three four five six seven"),
        ChatMessage(role="assistant", content="eight nine ten eleven twelve"),
        ChatMessage(role="user", content="thirteen fourteen"),
        ChatMessage(role="assistant", content="final answer words here"),
    ]


async def test_summarization_fires_above_trigger_replaces_head():
    summarizer = StubProvider(texts=["short summary"])
    mw = summarization_middleware(summarizer, trigger_tokens=5, keep_messages=2)
    req = ModelRequest(messages=long_messages(), tools=[])
    out = await mw.before_model(req, CTX)

    assert out is not None and out is not req
    assert len(summarizer.calls) == 1
    # the summary call carries the OLD HEAD only (never the kept tail)
    prompt = summarizer.calls[0].messages[-1].content
    assert "one two three four five six" in prompt
    assert "final answer" not in prompt
    assert "Summarize this conversation" in prompt

    # [system, summary-message, ...kept tail]
    assert out.messages[0].content == "sys"
    assert out.messages[1].role == "assistant"
    assert out.messages[1].content.startswith("Prior context summary\n")
    assert "short summary" in out.messages[1].content
    assert [m.content for m in out.messages[2:]] == ["thirteen fourteen", "final answer words here"]
    # the caller's list is never mutated in place
    assert len(req.messages) == 5


async def test_summarization_below_trigger_is_noop():
    summarizer = StubProvider(texts=["should never run"])
    mw = summarization_middleware(summarizer, trigger_tokens=1000, keep_messages=2)
    req = ModelRequest(messages=long_messages(), tools=[])
    assert await mw.before_model(req, CTX) is None
    assert summarizer.calls == []


async def test_summarization_noop_when_history_shorter_than_keep():
    summarizer = StubProvider(texts=["should never run"])
    mw = summarization_middleware(summarizer, trigger_tokens=0, keep_messages=10)
    req = ModelRequest(messages=long_messages(), tools=[])
    assert await mw.before_model(req, CTX) is None


# ---------------------------------------------------------------------------
# context editing: pairs stripped, keep_recent + pinned survive.
# ---------------------------------------------------------------------------


def pair_messages() -> list[ChatMessage]:
    return [
        ChatMessage(role="system", content="sys"),
        ChatMessage(role="user", content="q"),
        ChatMessage(
            role="assistant",
            content=[
                {"type": "tool_call", "id": "c1", "name": "search", "args": {"q": "old"}},
                {"type": "text", "text": "found old stuff"},
            ],
        ),
        ChatMessage(role="tool", content="old-result-1", tool_call_id="c1", tool_name="search"),
        ChatMessage(role="assistant", content=[{"type": "tool_call", "id": "c2", "name": "search", "args": {"q": "new"}}]),
        ChatMessage(role="tool", content="new-result-2", tool_call_id="c2", tool_name="search"),
    ]


async def test_context_editing_strips_old_pairs_keeps_keep_recent():
    mw = context_editing_middleware([ClearToolUsesEdit(keep_recent=1)])
    req = ModelRequest(messages=pair_messages(), tools=[])
    out = await mw.before_model(req, CTX)

    assert out is not None and out is not req
    # only the newest tool RESULT survives
    assert [m.content for m in out.messages if m.role == "tool"] == ["new-result-2"]
    # old assistant tool_call blocks are stripped too (the PAIR goes, the text stays)
    first_assistant = out.messages[2]
    blocks = first_assistant.blocks()
    assert [b.type for b in blocks] == ["text"] and blocks[0].text == "found old stuff"
    # the newest assistant tool_call survives untouched
    second_assistant = out.messages[3]
    assert [b.id for b in second_assistant.blocks() if b.type == "tool_call"] == ["c2"]
    # the caller's list is untouched
    assert len(req.messages) == 6
    # head-of-conversation text survives
    assert out.messages[0].content == "sys" and out.messages[1].content == "q"


async def test_context_editing_unprotected_pairs_fully_removed():
    mw = context_editing_middleware([ClearToolUsesEdit()])
    out = await mw.before_model(
        ModelRequest(
            messages=[
                ChatMessage(role="user", content="q"),
                ChatMessage(role="assistant", content=[{"type": "tool_call", "id": "c1", "name": "t", "args": {}}]),
                ChatMessage(role="tool", content="r1", tool_call_id="c1", tool_name="t"),
            ],
            tools=[],
        ),
        CTX,
    )
    # keep_recent=0: the call-only assistant message AND the result are gone
    assert [m.role for m in out.messages] == ["user"]


async def test_context_editing_pinned_pair_survives():
    messages = pair_messages()
    messages[3].pinned = True  # pin the OLD tool result
    mw = context_editing_middleware([ClearToolUsesEdit()])
    out = await mw.before_model(ModelRequest(messages=messages, tools=[]), CTX)
    # pinned result protects its call; the unpinned pair is gone entirely
    assert [m.content for m in out.messages if m.role == "tool"] == ["old-result-1"]
    assert [b.id for b in out.messages[2].blocks() if b.type == "tool_call"] == ["c1"]


async def test_context_editing_older_than_steps_gates_and_unchanged_returns_none():
    mw = context_editing_middleware([ClearToolUsesEdit(older_than_steps=2)])
    req = ModelRequest(messages=pair_messages(), tools=[])
    assert await mw.before_model(req, HookContext(step=1)) is None  # below the gate
    out = await mw.before_model(req, HookContext(step=2))
    assert out is not None and len(out.messages) < len(req.messages)

    # every edit a no-op -> the request passes through untouched
    noop = context_editing_middleware([ClearToolUsesEdit()])
    assert await noop.before_model(ModelRequest(messages=[ChatMessage(role="user", content="q")], tools=[]), CTX) is None


# ---------------------------------------------------------------------------
# llm tool selector: picks filter, garbage + failure degrade, forced names.
# ---------------------------------------------------------------------------


def wide_req() -> ModelRequest:
    return ModelRequest(
        messages=[ChatMessage(role="user", content="do stuff")],
        tools=[
            ToolWire(name="alpha", description="a-tool"),
            ToolWire(name="beta", description="b-tool"),
            ToolWire(name="gamma", description="c-tool"),
        ],
    )


async def test_llm_selector_filters_to_picked_subset():
    selector = StubProvider(texts=["beta, gamma"])
    mw = llm_tool_selector_middleware(selector)
    out = await mw.before_model(wide_req(), CTX)
    assert out is not None
    assert [t.name for t in out.tools] == ["beta", "gamma"]
    assert [m.content for m in out.messages] == ["do stuff"]
    # the menu lists names + descriptions and carries the last user query
    prompt = selector.calls[0].messages[-1].content
    assert "alpha" in prompt and "a-tool" in prompt and "do stuff" in prompt
    assert selector.calls[0].messages[0].role == "system"


async def test_llm_selector_garbage_reply_degrades_to_unfiltered():
    selector = StubProvider(texts=["I cannot choose, sorry!"])
    mw = llm_tool_selector_middleware(selector)
    assert await mw.before_model(wide_req(), CTX) is None  # original tools stand


async def test_llm_selector_model_failure_degrades_to_unfiltered():
    selector = StubProvider(error=RuntimeError("selector down"))
    mw = llm_tool_selector_middleware(selector)
    assert await mw.before_model(wide_req(), CTX) is None


async def test_llm_selector_always_include_forced_skipped_under_max_tools():
    # always_include survives even when the picker omits the name
    selector = StubProvider(texts=["alpha"])
    mw = llm_tool_selector_middleware(selector, always_include=["gamma"])
    out = await mw.before_model(wide_req(), CTX)
    assert out is not None
    assert {t.name for t in out.tools} == {"alpha", "gamma"}

    # few enough tools + no forced names -> the selector never runs
    quiet = StubProvider(texts=["beta"])
    mw = llm_tool_selector_middleware(quiet, max_tools=5)
    assert await mw.before_model(wide_req(), CTX) is None
    assert quiet.calls == []

    # forced names + few tools still run the selector
    forced = StubProvider(texts=["beta"])
    mw = llm_tool_selector_middleware(forced, max_tools=5, always_include=["gamma"])
    out = await mw.before_model(wide_req(), CTX)
    assert {t.name for t in out.tools} == {"beta", "gamma"}


async def test_llm_selector_via_offered_tools_and_max_tools_slice():
    selector = StubProvider(texts=["alpha, beta, gamma"])
    mw = llm_tool_selector_middleware(selector, max_tools=2)
    out = await mw.before_model(wide_req(), CTX)
    assert [t.name for t in out.tools] == ["alpha", "beta"]  # sliced to max_tools


# ---------------------------------------------------------------------------
# tool emulator: emulated string, real execute skipped, others pass through.
# ---------------------------------------------------------------------------


def real_counter_tool() -> tuple[Any, dict[str, int]]:
    ran = {"n": 0}

    class Args(BaseModel):
        text: str = ""

    async def execute(args: Any, ctx: ToolContext | None) -> Any:
        ran["n"] += 1
        return f"real:{args.text}"

    return define_tool(name="search", description="searches things", schema=Args, execute=execute), ran


async def test_tool_emulator_returns_emulated_string_without_executing():
    tool, ran = real_counter_tool()
    emulator = StubProvider(texts=["emulated-output"])
    mw = tool_emulator_middleware(emulator)

    # descriptions are recorded from before_model (the menu the model sees)
    req = ModelRequest(messages=[], tools=[ToolWire(name="search", description="searches things", parameters_schema={})])
    mw.before_model(req, CTX)  # sync hook (the loop's _maybe_await unwraps either way)

    async def next_fn() -> Any:
        return await tool.execute(tool.schema.model_validate({"text": "q"}), None)

    out = await mw.around_tool(ToolCallRef(name="search", args={"text": "q"}), next_fn, CTX)
    assert out == "emulated-output"
    assert ran["n"] == 0  # never executed
    # the prompt carries the tool name, its description, and the call args
    prompt = emulator.calls[0].messages[0].content
    assert 'Emulate tool "search" (searches things)' in prompt
    assert '{"text": "q"}' in prompt


async def test_tool_emulator_passes_through_unlisted_tools():
    tool, ran = real_counter_tool()
    emulator = StubProvider(texts=["emulated"])
    mw = tool_emulator_middleware(emulator, tools=["other"])

    async def next_fn() -> Any:
        ran["n"] += 1
        return "real result"

    out = await mw.around_tool(ToolCallRef(name="search", args={}), next_fn, CTX)
    assert out == "real result"
    assert ran["n"] == 1
    assert emulator.calls == []


# ---------------------------------------------------------------------------
# filesystem tools: roundtrip, edit, escape feedback (never a raise).
# ---------------------------------------------------------------------------


async def fs_call(tool: Any, args: dict[str, Any]) -> str:
    return await tool.execute(tool.schema.model_validate(args), None)


def fs_tools(tmp_path: Any) -> dict[str, Any]:
    return {t.name: t for t in filesystem_tools(tmp_path)}


async def test_filesystem_tools_roundtrip_and_edit(tmp_path):
    tools = fs_tools(tmp_path)
    assert sorted(tools) == ["edit_file", "ls", "read_file", "write_file"]

    # write creates parents and reports the length
    out = await fs_call(tools["write_file"], {"path": "notes/a.txt", "content": "hello world"})
    assert out == "wrote notes/a.txt (11 chars)"
    assert (tmp_path / "notes" / "a.txt").read_text() == "hello world"

    assert await fs_call(tools["read_file"], {"path": "notes/a.txt"}) == "hello world"

    # edit replaces the FIRST occurrence only
    await fs_call(tools["write_file"], {"path": "b.txt", "content": "foo bar foo"})
    out = await fs_call(tools["edit_file"], {"path": "b.txt", "old_string": "foo", "new_string": "baz"})
    assert out == "edited b.txt"
    assert (tmp_path / "b.txt").read_text() == "baz bar foo"

    # edit miss is a feedback STRING, not a raise
    out = await fs_call(tools["edit_file"], {"path": "b.txt", "old_string": "absent", "new_string": "x"})
    assert "oldString not found" in out and "b.txt" in out

    ls_out = await fs_call(tools["ls"], {})
    assert ls_out == "b.txt\nnotes"


async def test_filesystem_tools_reject_escape_with_feedback(tmp_path):
    tools = fs_tools(tmp_path)
    root = str(tmp_path)
    assert root not in ("", "/")  # sanity: ../ must escape this root

    # read escape -> feedback string, no raise
    out = await fs_call(tools["read_file"], {"path": "../evil.txt"})
    assert "escape" in out and "../evil.txt" in out

    # write escape -> feedback string, nothing created outside the root
    out = await fs_call(tools["write_file"], {"path": "../escape.txt", "content": "nope"})
    assert "escape" in out
    assert not (tmp_path.parent / "escape.txt").exists()

    # absolute paths also stay caged
    out = await fs_call(tools["read_file"], {"path": "/etc/passwd"})
    assert "escape" in out


async def test_filesystem_middleware_contributes_tools(tmp_path):
    mw = filesystem_middleware(tmp_path)
    assert mw.name == "filesystem"
    assert [t.name for t in mw.tools] == ["ls", "read_file", "write_file", "edit_file"]

    # as an Agent middleware the escape surfaces as model-visible feedback
    model = ScriptedModelProvider(
        [
            ModelCompletion(tool_calls=[ToolCallRequest(id="c1", name="read_file", args={"path": "../evil.txt"})]),
            ModelCompletion(text="done"),
        ]
    )
    agent = Agent(AgentOptions(model=model, middleware=[mw]))
    result = await agent.run("go")
    assert result.tool_calls[0].name == "read_file"
    assert "escape" in result.tool_calls[0].output
    assert "escape" in [m for m in result.messages if m.role == "tool"][0].content


# ---------------------------------------------------------------------------
# Subagents: task dispatch, unknown-name feedback, fresh Agent per call.
# ---------------------------------------------------------------------------


WX_DEF = {"name": "wx", "description": "weather", "system_prompt": "you report weather", "tools": []}


async def test_subagent_task_tool_dispatches_to_registered_agent():
    mw = create_sub_agent_middleware(ScriptedModelProvider([ModelCompletion(text="sunny")]), [WX_DEF])
    task = mw.tools[0]
    assert task.name == "task"
    assert "wx (weather)" in task.description

    out = await task.execute(task.schema.model_validate({"subagent": "wx", "input": "weather?"}), None)
    assert out == "sunny"


async def test_subagent_unknown_name_returns_feedback():
    mw = create_sub_agent_middleware(ScriptedModelProvider([ModelCompletion(text="s")]), [])
    task = mw.tools[0]
    out = await task.execute(task.schema.model_validate({"subagent": "ghost", "input": "x"}), None)
    assert "unknown subagent" in out and "ghost" in out  # model-visible, no raise


async def test_subagent_built_fresh_per_call_with_parent_model_param():
    sub_model = ScriptedModelProvider([ModelCompletion(text="answer-1")])
    mw = create_sub_agent_middleware(sub_model, [WX_DEF])
    task = mw.tools[0]
    first = await task.execute(task.schema.model_validate({"subagent": "wx", "input": "a"}), None)
    second = await task.execute(task.schema.model_validate({"subagent": "wx", "input": "b"}), None)
    assert first == second == "answer-1"  # fresh Agent per call, same scripted model
    assert sub_model._n == 2


async def test_subagent_via_agent_loop():
    parent = ScriptedModelProvider(
        [
            ModelCompletion(tool_calls=[ToolCallRequest(id="c1", name="task", args={"subagent": "wx", "input": "weather?"})]),
            ModelCompletion(text="done"),
        ]
    )
    mw = create_sub_agent_middleware(ScriptedModelProvider([ModelCompletion(text="sunny")]), [WX_DEF])
    agent = Agent(AgentOptions(model=parent, middleware=[mw]))
    result = await agent.run("go")
    assert result.tool_calls[0].output == "sunny"
    assert result.text == "done"


# ---------------------------------------------------------------------------
# PII: apply_to_tool_results string scrubbing.
# ---------------------------------------------------------------------------


async def test_pii_apply_to_tool_results():
    mw = pii_redact("api_key", detector="sk-[a-z0-9]+", apply_to_output=False, apply_to_tool_results=True)
    out = await mw.after_tool(ToolCallRef(name="leak", args={}), "key sk-abc123", CTX)
    assert out == "key [REDACTED_API_KEY]"

    # off by default
    off = pii_redact("email")
    assert await off.after_tool(ToolCallRef(name="x", args={}), "a@b.com", CTX) is None

    # non-string outputs are untouched
    assert await mw.after_tool(ToolCallRef(name="x", args={}), {"obj": 1}, CTX) is None


async def test_pii_tool_results_via_agent_loop():
    class LeakArgs(BaseModel):
        pass

    async def leak(args: Any, ctx: ToolContext | None) -> Any:
        return "key sk-abc123"

    leak_tool = define_tool(name="leak", description="l", schema=LeakArgs, execute=leak)
    model = ScriptedModelProvider(
        [
            ModelCompletion(tool_calls=[ToolCallRequest(id="c1", name="leak", args={})]),
            ModelCompletion(text="done"),
        ]
    )
    agent = Agent(
        AgentOptions(
            model=model,
            tools=[leak_tool],
            middleware=[pii_redact("api_key", detector="sk-[a-z0-9]+", apply_to_output=False, apply_to_tool_results=True)],
        )
    )
    result = await agent.run("go")
    tool_msg = [m for m in result.messages if m.role == "tool"][0]
    assert tool_msg.content == "key [REDACTED_API_KEY]"
    assert result.tool_calls[0].output == "key [REDACTED_API_KEY]"