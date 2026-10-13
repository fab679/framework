"""Multi-agent parity ports: orchestration, workflows, swarm, evals, skills,
prompts, RAG. Stub-model only — no network."""

import asyncio
import json

import pytest
from pydantic import BaseModel

from framework_agents import (
    Agent,
    AgentError,
    AgentOptions,
    AgentResult,
    ModelCompletion,
    ScriptedModelProvider,
    TokenUsage,
    ToolCallRequest,
    ToolRegistry,
    define_tool,
)
from framework_agents.evals import (
    EvalCase,
    EvalExpect,
    load_jsonl,
    print_report,
    run_evals,
)
from framework_agents.orchestration import OrchestrationPlan, run_orchestrated
from framework_agents.prompts import PromptTemplate, ab_prompts, load_prompt_file
from framework_agents.rag import InMemoryRetriever, retrieval_tool
from framework_agents.skills import (
    Skill,
    SkillRegistry,
    discover_skills,
    match_skills,
    parse_skill_file,
    score_skills,
    tokenize,
)
from framework_agents.swarm import Swarm, handoff_signal, read_handoff_signal
from framework_agents.workflows import RetryPolicy, WorkflowNode, WorkflowOptions, run_workflow


class RecordingModel:
    """Stub provider that records the CompleteOptions it receives."""

    id = "recording"
    capabilities = {"structured_output": False, "streaming": False}

    def __init__(self, text: str, completions: list[ModelCompletion] | None = None) -> None:
        self._text = text
        self._completions = list(completions or [])
        self.calls: list = []

    async def complete(self, options) -> ModelCompletion:
        # snapshot: the agent keeps appending to the same messages list
        self.calls.append(list(options.messages))
        if self._completions:
            completion = self._completions[min(len(self.calls) - 1, len(self._completions) - 1)]
            return ModelCompletion(
                text=completion.text,
                thinking=completion.thinking,
                tool_calls=completion.tool_calls,
                usage=completion.usage,
                finish_reason=completion.finish_reason,
            )
        return ModelCompletion(text=self._text, usage=TokenUsage(input_tokens=7, output_tokens=3))


def make_agent(name: str, model, description: str = "") -> Agent:
    return Agent(AgentOptions(model=model, name=name, description=description))


# ---- orchestration ----------------------------------------------------


async def test_sequential_chaining_feeds_agent2_from_agent1():
    model1 = RecordingModel("first draft")
    model2 = RecordingModel("chained final")
    a1 = make_agent("draft", model1)
    a2 = make_agent("refine", model2)

    result = await run_orchestrated(OrchestrationPlan(strategy="sequential", agents=[a1, a2]), "seed input")

    assert result.strategy == "sequential"
    assert result.text == "chained final"  # combined text = LAST result
    assert [r.text for r in result.results] == ["first draft", "chained final"]
    agent2_input = model2.calls[0][-1]
    assert agent2_input.role == "user"
    assert agent2_input.content == "seed input\n\nPrevious result (draft):\nfirst draft"


class StubAgent:
    """Duck-typed agent that tracks overlap around a sleep."""

    def __init__(self, name: str, text: str, tracker: dict, delay: float = 0.02) -> None:
        self.name = name
        self._text = text
        self._tracker = tracker
        self._delay = delay

    async def run(self, _input: str) -> AgentResult:
        self._tracker["active"] += 1
        self._tracker["peak"] = max(self._tracker["peak"], self._tracker["active"])
        await asyncio.sleep(self._delay)
        self._tracker["active"] -= 1
        return AgentResult(text=self._text, messages=[], tool_calls=[], usage=TokenUsage())


async def test_parallel_keeps_agent_order_and_caps_concurrency():
    tracker = {"active": 0, "peak": 0}
    agents = [StubAgent(f"a{i}", f"agent-{i}-out", tracker) for i in range(5)]

    result = await run_orchestrated(
        OrchestrationPlan(strategy="parallel", agents=agents, concurrency=2), "fan out"
    )

    assert result.strategy == "parallel"
    # results keep AGENT order, not completion order
    assert [r.text for r in result.results] == [f"agent-{i}-out" for i in range(5)]
    assert result.text == "\n\n".join(f"agent-{i}-out" for i in range(5))
    assert tracker["peak"] == 2  # capped at 2, and both slots really overlap


async def test_router_picks_index_1_and_runs_only_that_agent():
    router = RecordingModel("I'd say agent 1 is best")
    summarizer = make_agent("summarizer", RecordingModel("summary text"), "condenses text")
    researcher = make_agent("researcher", RecordingModel("research output"), "finds sources")

    plan = OrchestrationPlan(strategy="router", agents=[summarizer, researcher], router_model=router)
    result = await run_orchestrated(plan, "find me sources")

    assert result.strategy == "router"
    assert result.routed_to == 1  # absolute index
    assert len(result.results) == 1  # ONLY the routed run
    assert result.results[0].text == "research output"
    assert result.text == "research output"
    routed_menu = router.calls[0][1].content
    assert "0: summarizer" in routed_menu
    assert "1: researcher" in routed_menu
    assert "find me sources" in routed_menu


async def test_router_no_match_raises():
    router = RecordingModel("words without digits")
    plan = OrchestrationPlan(strategy="router", agents=[make_agent("a", RecordingModel("x"))], router_model=router)
    with pytest.raises(AgentError) as excinfo:
        await run_orchestrated(plan, "anything")
    assert excinfo.value.code == "ROUTER_NO_MATCH"


async def test_router_out_of_range_index_raises():
    router = RecordingModel("agent 7")
    plan = OrchestrationPlan(
        strategy="router",
        agents=[make_agent("a", RecordingModel("x")), make_agent("b", RecordingModel("y"))],
        router_model=router,
    )
    with pytest.raises(AgentError) as excinfo:
        await run_orchestrated(plan, "anything")
    assert excinfo.value.code == "ROUTER_NO_MATCH"


async def test_orchestration_empty_plan_raises():
    with pytest.raises(AgentError) as excinfo:
        await run_orchestrated(OrchestrationPlan(strategy="sequential", agents=[]), "x")
    assert excinfo.value.code == "ORCHESTRATION_EMPTY"


# ---- workflows ---------------------------------------------------------


async def test_workflow_duplicate_dep_is_one_edge():
    """Pinned TS regression: depends_on ["a", "a"] is one edge, not a cycle."""
    ran: list[str] = []

    async def node_a(ctx, state):
        ran.append("a")
        return "A"

    async def node_b(ctx, state):
        ran.append("b")
        assert ctx["a"] == "A"
        return "B"

    async def node_c(ctx, state):
        ran.append("c")
        assert ctx == {"a": "A", "b": "B"}
        return "C"

    nodes = [
        WorkflowNode(id="a", run=node_a),
        WorkflowNode(id="b", run=node_b, depends_on=["a", "a"]),
        WorkflowNode(id="c", run=node_c, depends_on=["a", "b"]),
    ]
    out = await run_workflow(nodes)
    assert out == {"a": "A", "b": "B", "c": "C"}
    assert ran.index("b") < ran.index("c")


async def test_workflow_error_propagates():
    async def bad(ctx, state):
        raise ValueError("boom")

    async def sibling(ctx, state):
        await asyncio.sleep(0.01)
        return "never mind"

    nodes = [WorkflowNode(id="bad", run=bad), WorkflowNode(id="other", run=sibling)]
    with pytest.raises(ValueError, match="boom"):
        await run_workflow(nodes)


async def test_workflow_retry_second_attempt_really_calls_again():
    calls: list[int] = []

    async def flaky(ctx, state):
        calls.append(1)
        if len(calls) < 2:
            raise RuntimeError("transient")
        return "recovered"

    out = await run_workflow(
        [WorkflowNode(id="flaky", run=flaky, retry=RetryPolicy(attempts=2, backoff_ms=5))]
    )
    assert out == {"flaky": "recovered"}
    assert len(calls) == 2  # the retry actually re-invoked the node


async def test_workflow_abort_never_retries():
    calls: list[int] = []

    async def aborting(ctx, state):
        calls.append(1)
        raise AgentError("ABORTED", "client aborted", retryable=False)

    nodes = [WorkflowNode(id="x", run=aborting, retry=RetryPolicy(attempts=3))]
    with pytest.raises(AgentError) as excinfo:
        await run_workflow(nodes)
    assert excinfo.value.code == "ABORTED"
    assert len(calls) == 1


async def test_workflow_validation_errors():
    async def noop(ctx, state):
        return None

    with pytest.raises(AgentError, match="unknown node"):
        await run_workflow([WorkflowNode(id="x", run=noop, depends_on=["ghost"])])
    with pytest.raises(AgentError) as excinfo:
        await run_workflow([WorkflowNode(id="x", run=noop, depends_on=["x"])])
    assert excinfo.value.code == "WORKFLOW_CYCLE"
    with pytest.raises(AgentError) as excinfo:
        await run_workflow([WorkflowNode(id="x", run=noop), WorkflowNode(id="x", run=noop)])
    assert excinfo.value.code == "WORKFLOW_DUPLICATE_ID"
    with pytest.raises(AgentError) as excinfo:
        await run_workflow(
            [
                WorkflowNode(id="x", run=noop, depends_on=["y"]),
                WorkflowNode(id="y", run=noop, depends_on=["x"]),
            ]
        )
    assert excinfo.value.code == "WORKFLOW_CYCLE"


async def test_workflow_invocation_state_reaches_nodes():
    seen = []

    async def node(ctx, state):
        seen.append(state)
        return "done"

    await run_workflow(
        [WorkflowNode(id="n", run=node)], WorkflowOptions(invocation_state={"trace_id": "t-42"})
    )
    assert seen == [{"trace_id": "t-42"}]


# ---- swarm -------------------------------------------------------------


def handoff_completion(to: str, message: str) -> ModelCompletion:
    return ModelCompletion(
        text="",
        tool_calls=[
            ToolCallRequest(id="call_handoff", name="handoff_to_agent", args={"to": to, "message": message})
        ],
        usage=TokenUsage(input_tokens=11, output_tokens=7),
    )


def swarm_pair() -> list[Agent]:
    researcher = make_agent(
        "researcher",
        ScriptedModelProvider([handoff_completion("writer", "draft the report")]),
        "gathers sources",
    )
    writer = make_agent(
        "writer",
        ScriptedModelProvider(
            [ModelCompletion(text="The report is written.", usage=TokenUsage(input_tokens=5, output_tokens=9))]
        ),
        "writes reports",
    )
    return [researcher, writer]


async def test_swarm_handoff_researcher_to_writer_completes():
    swarm = Swarm(swarm_pair())
    events = []
    collected = None
    async for event in swarm.stream("write a report"):
        events.append(event)
        if event["type"] == "result":
            collected = event["result"]

    assert collected is not None
    assert collected.status == "COMPLETED"
    assert collected.text == "The report is written."
    assert collected.node_history == ["researcher", "writer"]
    assert collected.handoffs == 1
    assert collected.iterations == 2
    assert collected.error is None
    assert set(collected.results) == {"writer"}
    assert collected.results["writer"].text == "The report is written."
    # only COMPLETED members contribute usage — the researcher's handoff turn has none
    assert collected.usage.input_tokens == 5
    assert collected.usage.output_tokens == 9

    handoffs = [e for e in events if e["type"] == "handoff"]
    assert handoffs == [{"type": "handoff", "from": "researcher", "to": "writer"}]
    assert [e["type"] for e in events].count("node_start") == 2
    assert [e["type"] for e in events].count("node_done") == 1
    assert [e["type"] for e in events].count("result") == 1
    wrapped = [e for e in events if e["type"] == "agent_event"]
    assert wrapped and all(e["agent"] in {"researcher", "writer"} for e in wrapped)


async def test_swarm_run_drains_and_returns_result():
    result = await Swarm(swarm_pair()).run("write a report")
    assert result.status == "COMPLETED"
    assert result.text == "The report is written."
    assert result.node_history == ["researcher", "writer"]


async def test_swarm_unknown_handoff_target_feeds_error_back_to_model():
    stubborn = make_agent(
        "stubborn",
        ScriptedModelProvider(
            [
                handoff_completion("ghost", "please?"),
                ModelCompletion(text="fine, I will do it myself"),
            ]
        ),
    )
    swarm = Swarm([stubborn])
    result = await swarm.run("task")
    assert result.status == "COMPLETED"
    assert result.handoffs == 0
    assert result.text == "fine, I will do it myself"


async def test_swarm_self_handoff_loop_detection_fails():
    loopy = make_agent("loopy", ScriptedModelProvider([handoff_completion("loopy", "again")]))
    swarm = Swarm([loopy], loop_detection_window=2)
    result = await swarm.run("task")
    assert result.status == "FAILED"
    assert result.error is not None and "Repetitive handoff" in result.error
    # pinned default: window > 0 alone enables detection with min 2 unique agents
    assert swarm._min_unique == 2


async def test_swarm_ping_pong_exceeds_max_handoffs():
    a = make_agent("a", ScriptedModelProvider([handoff_completion("b", "go")]))
    b = make_agent("b", ScriptedModelProvider([handoff_completion("a", "back")]))
    swarm = Swarm([a, b], max_handoffs=3)
    result = await swarm.run("task")
    assert result.status == "FAILED"
    assert result.error == "Exceeded maxHandoffs (3)"
    assert result.handoffs == 4  # the failing handoff is counted


async def test_swarm_entry_point_by_name():
    researcher, writer = swarm_pair()
    swarm = Swarm([researcher, writer], entry_point="writer")
    result = await swarm.run("short note")
    assert result.status == "COMPLETED"
    assert result.node_history == ["writer"]
    assert result.iterations == 1


def test_swarm_unknown_entry_point_raises():
    with pytest.raises(AgentError) as excinfo:
        Swarm(swarm_pair(), entry_point="ghost")
    assert excinfo.value.code == "SWARM_UNKNOWN_ENTRY"


def test_swarm_empty_raises():
    with pytest.raises(AgentError) as excinfo:
        Swarm([])
    assert excinfo.value.code == "SWARM_EMPTY"


def test_read_handoff_signal_roundtrip():
    err = handoff_signal("writer", "take over")
    signal = read_handoff_signal(err)
    assert signal is not None
    assert signal.to == "writer"
    assert signal.message == "take over"
    assert read_handoff_signal(ValueError("plain")) is None
    assert read_handoff_signal(AgentError("OTHER", "no payload")) is None


# ---- evals -------------------------------------------------------------


class _NoteArgs(BaseModel):
    text: str


async def _note_execute(args, ctx=None):
    return "noted"


async def test_evals_pass_rate_two_pass_one_fail(tmp_path, capsys):
    path = tmp_path / "evals.jsonl"
    cases_raw = [
        {"id": "greet", "input": "say hi", "expect": {"contains": ["hello"]}},
        {"id": "bye", "input": "say bye", "expect": {"contains": ["goodbye"]}},
        {"id": "miss", "input": "explain", "expect": {"contains": ["quantum"]}},
    ]
    path.write_text("\n".join(json.dumps(c) for c in cases_raw) + "\n", encoding="utf-8")

    cases = load_jsonl(path)
    assert [c.id for c in cases] == ["greet", "bye", "miss"]
    assert cases[0].expect.contains == ["hello"]

    replies = {"greet": "hello there", "bye": "goodbye friend", "miss": "classical mechanics"}
    run = await run_evals(cases, lambda c: make_agent(c.id, ScriptedModelProvider([ModelCompletion(text=replies[c.id])])))
    assert [s.id for s in run.scores if s.passed] == ["greet", "bye"]
    assert run.pass_rate == pytest.approx(2 / 3)
    assert "missing text" in run.scores[2].detail

    print_report(run.scores, run.pass_rate)
    out = capsys.readouterr().out
    assert "PASS  greet" in out
    assert "FAIL  miss" in out
    assert "pass rate: 66.7% (2/3)" in out


async def test_evals_tool_used_expected_and_throwing_cases():
    note_tool = define_tool(
        name="note",
        description="take a note",
        schema=_NoteArgs,
        execute=_note_execute,
    )
    tool_agent = Agent(
        AgentOptions(
            model=ScriptedModelProvider(
                [
                    ModelCompletion(
                        tool_calls=[ToolCallRequest(id="c1", name="note", args={"text": "hi"})]
                    ),
                    ModelCompletion(text="done, noted"),
                ]
            ),
            tools=[note_tool],
        )
    )

    cases = [
        EvalCase(id="tools", input="take a note", expect=EvalExpect(contains=["noted"], tool_used=["note"])),
        EvalCase(id="threw", input="explode"),
    ]

    def make(case):
        if case.id == "tools":
            return tool_agent
        raise RuntimeError("no agent available")

    run = await run_evals(cases, make)
    assert run.scores[0].passed is True
    assert run.scores[0].detail == "ok"
    assert run.scores[0].usage is not None
    assert run.scores[1].passed is False
    assert "threw:" in run.scores[1].detail


# ---- skills ------------------------------------------------------------


def make_skill(name: str, description: str) -> Skill:
    return Skill(name=name, description=description, version="0.1.0", path="<inline>", body="body")


def test_parse_skill_file_tolerates_bom_and_crlf():
    content = "﻿---\r\nname: art\r\ndescription: Drawing and painting\r\nversion: '1.2.3'\r\n---\r\n\r\nDraw things.\r\n"
    meta, body = parse_skill_file(content, "SKILL.md")
    assert meta["name"] == "art"
    assert meta["description"] == "Drawing and painting"
    assert meta["version"] == "1.2.3"
    assert body == "Draw things."
    # version defaults when missing
    meta2, _ = parse_skill_file("---\nname: x\ndescription: y\n---\n\nBody text.\n")
    assert meta2["version"] == "0.1.0"


def test_parse_skill_file_rejects_bad_input():
    with pytest.raises(ValueError, match="missing frontmatter"):
        parse_skill_file("no frontmatter here", "SKILL.md")
    with pytest.raises(ValueError, match="empty body"):
        parse_skill_file("---\nname: x\ndescription: y\n---\n\n", "SKILL.md")
    with pytest.raises(ValueError, match="missing name"):
        parse_skill_file("---\ndescription: y\n---\n\nBody.\n", "SKILL.md")


def test_tokenize_parity():
    assert tokenize("Hello, World-2!") == ["hello", "world"]
    assert tokenize("$Artillery_SUPPORT") == ["artillery", "support"]
    assert tokenize("a I be") == ["be"]  # single chars dropped, "be" kept


def test_skill_registry_duplicate_name_raises():
    registry = SkillRegistry()
    registry.register(make_skill("art", "drawing"))
    with pytest.raises(ValueError, match="already registered"):
        registry.register(make_skill("art", "other thing"))
    assert [s.name for s in registry.list()] == ["art"]
    assert registry.get("art").description == "drawing"
    assert registry.get("ghost") is None


def test_dollar_art_does_not_match_artillery():
    registry = SkillRegistry()
    registry.register(make_skill("art", "drawing and painting"))
    assert match_skills(registry, "call $artillery support now") == []
    assert match_skills(registry, "trigger $testing suite") == []  # no skill here either

    registry.register(make_skill("test", "running tests"))
    assert match_skills(registry, "trigger $testing suite") == []  # $testing != $test
    assert [s.name for s in match_skills(registry, "run the $test")] == ["test"]


def test_mention_routing_always_wins_first():
    registry = SkillRegistry()
    registry.register(make_skill("art", "drawing and painting"))
    registry.register(make_skill("ppt", "paint inspection forensics"))
    query = "help with paint inspection $art special forensics"
    matched = match_skills(registry, query)
    assert matched and matched[0].name == "art"  # mention beats tf-idf
    scores = {s.skill.name: s.score for s in score_skills(registry, query)}
    assert scores["art"] == float("inf")
    assert 0.0 < scores["ppt"] < float("inf")


def test_tfidf_ranking_over_description():
    registry = SkillRegistry()
    registry.register(make_skill("printer", "3d printing hardware calibration"))
    registry.register(make_skill("garden", "watering plants soil"))
    matched = match_skills(registry, "calibrate printing hardware please")
    assert [s.name for s in matched] == ["printer"]
    assert match_skills(registry, "weather balloons") == []


def test_discover_skills(tmp_path):
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "SKILL.md").write_text(
        "---\nname: pack\ndescription: the pack skill\nversion: 1.0.0\n---\n\nUse the pack.\n",
        encoding="utf-8",
    )
    (tmp_path / "loose_SKILL.md").write_text(
        "---\nname: loose\ndescription: inline skill\n---\n\nBody.\n", encoding="utf-8"
    )
    (tmp_path / "not_a_skill.md").write_text("nothing", encoding="utf-8")

    skills = discover_skills([str(tmp_path), str(tmp_path / "missing-dir")])
    assert {s.name for s in skills} == {"pack", "loose"}
    assert all(s.tools == [] for s in skills)

    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "SKILL.md").write_text("no frontmatter", encoding="utf-8")
    assert discover_skills([str(bad)]) == []  # skipped by default
    with pytest.raises(ValueError):
        discover_skills([str(bad)], strict=True)


# ---- prompts -----------------------------------------------------------


def test_prompt_render_and_missing_var_raises():
    template = PromptTemplate(name="greet", template="Hello {{name}}, you are {{ role }}!")
    assert template.render({"name": "Ada", "role": "admin"}) == "Hello Ada, you are admin!"
    with pytest.raises(ValueError, match="Missing prompt var: name"):
        template.render({"role": "admin"})
    with pytest.raises(ValueError, match="Missing prompt var: role"):
        template.render({"name": "Ada"})


def test_load_prompt_file_tolerates_bom_and_crlf(tmp_path):
    path = tmp_path / "PROMPT.md"
    path.write_text(
        "﻿---\r\nname: greeter\r\nversion: 2.0.0\r\n---\r\n\r\nHello {{name}}.\r\n",
        encoding="utf-8",
    )
    template = load_prompt_file(path)
    assert template.name == "greeter"
    assert template.version == "2.0.0"
    assert template.render({"name": "Ada"}) == "Hello Ada."

    versionless = tmp_path / "v.md"
    versionless.write_text("---\nname: plain\n---\n\nX {{n}}\n", encoding="utf-8")
    assert load_prompt_file(versionless).version == "0.1.0"

    bad = tmp_path / "bad.md"
    bad.write_text("no frontmatter", encoding="utf-8")
    with pytest.raises(ValueError, match="missing frontmatter"):
        load_prompt_file(bad)
    noname = tmp_path / "noname.md"
    noname.write_text("---\nversion: 1.0.0\n---\n\nBody.\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing name"):
        load_prompt_file(noname)


def test_ab_prompts_explicit_pick():
    variants = [
        PromptTemplate(name="a", template="A"),
        PromptTemplate(name="b", template="B", version="2.0.0"),
        PromptTemplate(name="c", template="C"),
    ]
    chosen = ab_prompts(variants, pick="b")
    assert chosen.template == "B"
    assert chosen.name == "b"
    assert chosen.version == "2.0.0"
    assert ab_prompts(variants, pick="b@2.0.0").template == "B"
    with pytest.raises(ValueError, match="Unknown prompt variant"):
        ab_prompts(variants, pick="zzz")


def test_ab_prompts_weighted_roll_is_unbiased_by_weight():
    variants = [
        PromptTemplate(name="a", template="A"),
        PromptTemplate(name="b", template="B"),
        PromptTemplate(name="c", template="C"),
    ]
    for _ in range(25):
        assert ab_prompts(variants, weights=[1, 0, 0]).name == "a"
        assert ab_prompts(variants, weights=[0, 1, 0]).name == "b"
        assert ab_prompts(variants, weights=[0, 0, 1]).name == "c"


# ---- rag ---------------------------------------------------------------


async def test_rag_returns_top_scored_doc():
    retriever = InMemoryRetriever(
        [
            {"id": "d1", "text": "quarterly revenue grew by 12 percent", "meta": {"kind": "finance"}},
            {"id": "d2", "text": "employee onboarding checklist"},
            {"id": "d3", "text": "revenue projection for next quarter"},
        ]
    )
    docs = await retriever.search("revenue growth")
    assert docs
    assert [d.score for d in docs] == sorted((d.score for d in docs), reverse=True)
    assert docs[0].score == max(d.score for d in docs)
    assert {d.id for d in docs} == {"d1", "d3"}  # the two revenue docs
    by_id = {d.id: d for d in docs}
    assert by_id["d1"].meta == {"kind": "finance"}
    assert by_id["d3"].meta is None
    assert all(d.score > 0 for d in docs)
    # default k = 4: everything that scored
    assert len(docs) == 2  # d2 has no overlap
    top_one = await retriever.search("revenue growth quarter", k=1)
    assert len(top_one) == 1
    assert top_one[0].id == "d3"  # matches revenue + quarter


async def test_rag_retrieval_tool_over_registry():
    retriever = InMemoryRetriever(
        [
            {"id": "d1", "text": "revenue growth figures"},
            {"id": "d2", "text": "onboarding guide for new hires"},
        ]
    )
    tool = retrieval_tool(retriever, name="retrieve", k=1)
    registry = ToolRegistry()
    registry.register(tool)

    args = tool.schema.model_validate({"query": "revenue"})
    direct = await tool.execute(args, None)
    assert isinstance(direct, list)
    assert direct[0].id == "d1"
    assert direct[0].score > 0

    raw = await registry.call("retrieve", {"query": "onboarding new hires"})
    assert [d.id for d in raw] == ["d2"]
    assert raw[0].score > 0

    with pytest.raises(AgentError) as excinfo:
        await registry.call("retrieve", {"query": ""})
    assert excinfo.value.code == "TOOL_ARG_INVALID"