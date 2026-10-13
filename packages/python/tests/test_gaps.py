"""TS-parity gap tests.

Covers the remaining ports: AgentState, agent timeline / edit_message /
branch / snapshots / pause+resume (incl. checkpointed HITL), session
restore-before-seed, v1 output, collect_stream, model capability registry,
a2a_as_tool, ollama(), provider_from_env.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
from typing import Any

import pytest
from pydantic import BaseModel

sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "src"))

from framework_agents import (  # noqa: E402
    Agent,
    AgentError,
    AgentEvents,
    AgentOptions,
    AgentState,
    FileCheckpointer,
    InMemoryMemory,
    InMemoryStorage,
    MockModelProvider,
    OllamaOptions,
    OllamaProvider,
    SessionManager,
    TokenBudgets,
    a2a_as_tool,
    assert_can_handle,
    collect_stream,
    define_tool,
    ollama,
    supports_vision,
)
from framework_agents.sessions import SnapshotData  # noqa: E402
from framework_agents.a2a_client import A2AClient  # noqa: E402
from framework_agents.middleware import (  # noqa: E402
    HitlInterrupt,
    human_in_the_loop,
    hitl_key,
)
from framework_agents.models import (  # noqa: E402
    CompleteOptions,
    ModelCompletion,
    ToolCallRequest,
)
from framework_agents.models.anthropic import AnthropicOptions, AnthropicProvider  # noqa: E402
from framework_agents.models.openai_compatible import (  # noqa: E402
    OpenAICompatibleOptions,
    OpenAICompatibleProvider,
    provider_from_env,
)
from framework_agents.types import ChatMessage, TokenUsage, assistant, user  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _EchoInput(BaseModel):
    text: str


def _echo_tool(name: str = "echo") -> Any:
    async def execute(args: Any, ctx: Any = None) -> Any:
        return f"echo:{args.text}"

    return define_tool(name=name, description="echo the input", schema=_EchoInput, execute=execute)


class RecordingProvider:
    """Model stub that records the messages it saw and replies deterministically."""

    id = "rec"
    capabilities = {"structured_output": False, "streaming": False}

    def __init__(self, thinking: str | None = None) -> None:
        self.seen: list[list[ChatMessage]] = []
        self.ids: list[str] = []
        self.thinking = thinking

    async def complete(self, options: CompleteOptions) -> ModelCompletion:
        self.seen.append(list(options.messages))
        self.ids.append(json.dumps([m.model_dump() for m in options.messages]))
        n = len(self.seen)
        return ModelCompletion(
            text=f"reply-{n}",
            thinking=self.thinking,
            usage=TokenUsage(input_tokens=1, output_tokens=1),
        )


class InterruptingProvider:
    """Issues a dangerous tool call until its result appears, then answers."""

    id = "danger"
    capabilities = {"structured_output": False, "streaming": False}

    def __init__(self, args_seq: list[dict]) -> None:
        self._args_seq = list(args_seq)
        self._n = 0

    async def complete(self, options: CompleteOptions) -> ModelCompletion:
        self._n += 1
        if any(m.role == "tool" and m.tool_name == "dangerous" for m in options.messages):
            return ModelCompletion(text="all done")
        args = self._args_seq[min(self._n - 1, len(self._args_seq) - 1)]
        return ModelCompletion(tool_calls=[ToolCallRequest(id="c1", name="dangerous", args=args)])


def _dangerous_tool() -> Any:
    async def execute(args: Any, ctx: Any = None) -> Any:
        return "ran"

    return define_tool(
        name="dangerous",
        description="danger",
        schema=_EchoInput,
        execute=execute,
    )


class _FakeResponse:
    status_code = 200
    text = ""


class _FakeOpenAIResponse(_FakeResponse):
    def json(self) -> dict[str, Any]:
        return {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }


class _FakeAnthropicResponse(_FakeResponse):
    def json(self) -> dict[str, Any]:
        return {
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }


class _FakeClient:
    def __init__(self, response: Any) -> None:
        self._response = response

    async def request(self, *args: Any, **kwargs: Any) -> Any:
        return self._response

    async def aclose(self) -> None:
        pass


# ---------------------------------------------------------------------------
# AgentState
# ---------------------------------------------------------------------------


class _MapLike:
    """Custom-prototype instance (a Python stand-in for the TS Map case)."""


async def test_state_rejects_custom_class_instances():
    state = AgentState()
    with pytest.raises(ValueError) as err:
        state.set("bad", _MapLike())
    assert "custom prototype" in str(err.value)


async def test_state_rejects_map_like_containers_and_functions():
    state = AgentState()
    with pytest.raises(ValueError):
        state.set("set", {"a"})  # set: JSON.stringify would lose it
    with pytest.raises(ValueError):
        state.set("fn", lambda: 1)  # functions never round-trip
    with pytest.raises(ValueError):
        AgentState({"bad": object()})


async def test_state_accepts_plain_json_and_roundtrips():
    state = AgentState({"theme": "dark"})
    state.set("items", [1, "two", {"three": 3.5}])
    state.set("flag", True)
    assert state.get("theme") == "dark"
    dumped = state.to_json()
    assert isinstance(dumped, dict) and dumped["theme"] == "dark"
    restored = AgentState.from_json(dumped)
    assert restored.get() == state.get()
    state.delete("theme")
    assert state.get("theme") is None
    assert state.get("items") == [1, "two", {"three": 3.5}]


async def test_agent_converts_dict_state_and_keeps_it_out_of_the_model():
    recorded = RecordingProvider()
    agent = Agent(AgentOptions(model=recorded, state={"secret": "swordfish"}))
    assert agent.state.get("secret") == "swordfish"
    await agent.run("hi")
    # State is NEVER sent to the model.
    assert "swordfish" not in "".join(recorded.ids)


# ---------------------------------------------------------------------------
# session_id, timeline, edit_message
# ---------------------------------------------------------------------------


async def test_session_id_property():
    agent = Agent(AgentOptions(model=MockModelProvider()))
    first = agent.session_id
    assert first == agent.session_id  # stable per-instance
    assert len(first) == 8 and int(first, 16) >= 0  # random hex id
    sm = SessionManager(InMemoryStorage(), session_id="sess-42")
    other = Agent(AgentOptions(model=MockModelProvider(), session_manager=sm))
    assert other.session_id == "sess-42"


async def test_edit_message_updates_history_memory_and_timeline():
    memory = InMemoryMemory()
    agent = Agent(AgentOptions(model=MockModelProvider(), memory=memory))
    await agent.run("hi")
    updated = await agent.edit_message(1, "edited-input")  # system=0, user=1
    assert updated.content == "edited-input"
    assert agent.messages[1].content == "edited-input"
    # memory stays in sync (clear-then-save of the non-system history)
    assert [m.content for m in await memory.load()] == ["edited-input", "mock-response-1"]
    assert [e["kind"] for e in agent.timeline] == ["run", "edit"]
    entry = agent.timeline[-1]
    assert entry["rev"] == 2 and entry["label"].startswith("message")


async def test_edit_message_guards():
    agent = Agent(AgentOptions(model=MockModelProvider()))
    await agent.run("hi")
    with pytest.raises(AgentError) as err:
        await agent.edit_message("msg_missing", "no")
    assert err.value.code == "MESSAGE_NOT_FOUND"
    with pytest.raises(AgentError) as err:
        await agent.edit_message(99, "no")
    assert err.value.code == "MESSAGE_NOT_FOUND"


async def test_edit_message_refuses_messages_with_tool_calls():
    model = InterruptingProvider([{"text": "prod"}])
    agent = Agent(AgentOptions(model=model, tools=[_dangerous_tool()]))
    result = await agent.run("go")
    idx = next(
        i
        for i, m in enumerate(result.messages)
        if m.role == "assistant" and not isinstance(m.content, str)
    )
    with pytest.raises(AgentError) as err:
        await agent.edit_message(idx, "cannot touch this")
    assert err.value.code == "MESSAGE_NOT_EDITABLE"


# ---------------------------------------------------------------------------
# branch / with_tools
# ---------------------------------------------------------------------------


async def test_branch_shares_config_copies_state_and_records_both_sides():
    provider = MockModelProvider()
    budgets = TokenBudgets(max_tokens=10_000)
    events = AgentEvents()
    agent = Agent(
        AgentOptions(
            model=provider,
            budgets=budgets,
            events=events,
            tools=[_echo_tool()],
            output_version="v1",
        )
    )
    await agent.run("hi")
    agent.state.set("key", "value")
    child = await agent.branch("experiment-b")
    assert child.options.budgets is budgets  # shared
    assert child.options.events is events
    assert child.options.output_version == "v1"
    assert [t.name for t in child._tools] == ["echo"]
    # history inherited + a copy of the timeline; branch recorded on BOTH sides
    assert [m.content for m in child.messages] == [m.content for m in agent.messages]
    assert agent.timeline[-1]["kind"] == "branch" and agent.timeline[-1]["label"] == "experiment-b"
    # same rev/kind/label recorded on both sides (only "at" timestamps differ)
    assert {k: v for k, v in child.timeline[-1].items() if k != "at"} == {
        k: v for k, v in agent.timeline[-1].items() if k != "at"
    }
    assert child.timeline is not agent.timeline
    # state COPIED via to_json (mutations do not leak back)
    child.state.set("key", "other")
    assert agent.state.get("key") == "value"
    assert child.options.session_manager is None
    # memory: fresh InMemoryMemory seeded with the non-system history
    seeded = [m.content for m in await child.options.memory.load()]
    assert seeded == ["hi", "mock-response-1"]


async def test_branch_overrides_apply():
    agent = Agent(AgentOptions(model=MockModelProvider(), name="orig"))
    await agent.run("hi")
    child = await agent.branch("override-b", {"name": "renamed"})
    assert child.name == "renamed"


async def test_with_tools_clones_and_dedupes():
    events = AgentEvents()
    budgets = TokenBudgets(max_tokens=5)
    agent = Agent(
        AgentOptions(
            model=MockModelProvider(),
            tools=[_echo_tool("echo")],
            budgets=budgets,
            events=events,
            memory=InMemoryMemory(),
            max_tool_calls=7,
        )
    )
    agent.state.set("a", 1)
    clone = agent.with_tools(_echo_tool("echo"), _echo_tool("other"), _echo_tool("echo",))
    # dedupe by name via the constructor (first occurrence wins)
    names = [t.name for t in clone._tools]
    assert names == ["echo", "other"]
    assert len([n for n in names if n == "echo"]) == 1
    # carried: budgets/events/max_tool_calls/middleware/response_format/output_version
    assert clone.options.budgets is budgets
    assert clone.options.events is events
    assert clone.options.max_tool_calls == 7
    assert clone.options.output_version == "v0"
    # NOT carried: memory, state, session
    assert clone.options.memory is None
    assert clone.state.get() == {}
    assert clone.options.session_manager is None


# ---------------------------------------------------------------------------
# snapshots
# ---------------------------------------------------------------------------


async def test_snapshot_roundtrip_restores_state_and_messages():
    memory = InMemoryMemory()
    agent = Agent(AgentOptions(model=MockModelProvider(), memory=memory))
    await agent.run("hello")
    agent.state.set("k", "v")
    snapshot = agent.take_snapshot()
    assert snapshot.id.startswith("snapshot-")
    assert snapshot.data.state == {"k": "v"}
    assert [m.content for m in snapshot.data.messages] == [m.content for m in agent.messages]

    # disturb everything, then load back
    agent.state.set("k", "changed")
    agent.state.set("extra", 1)
    agent._history = []
    await memory.clear()
    await agent.load_snapshot(snapshot)
    assert agent.state.get("k") == "v"  # merged key-by-key
    assert agent.state.get("extra") == 1  # absent keys keep current values
    assert [m.content for m in agent.messages] == [m.content for m in snapshot.data.messages]
    assert [m.content for m in await memory.load()] == ["hello", "mock-response-1"]


async def test_snapshot_field_selection():
    agent = Agent(AgentOptions(model=MockModelProvider()))
    await agent.run("hello")
    snapshot = agent.take_snapshot({"preset": "session", "exclude": ["messages"]})
    assert snapshot.data.messages is None
    assert snapshot.data.state == {}
    agent._history = []
    await agent.load_snapshot(snapshot)
    assert agent.messages == []  # messages absent -> untouched


# ---------------------------------------------------------------------------
# sessions: restore-before-seed + save-after-run
# ---------------------------------------------------------------------------


async def test_session_restore_wins_over_initial_messages():
    storage = InMemoryStorage()
    sm = SessionManager(storage, session_id="sess-1")
    await sm.save_latest(
        SnapshotData(
            messages=[user("persisted question"), assistant("persisted answer")],
            state={"theme": "dark"},
        )
    )
    agent = Agent(
        AgentOptions(
            model=MockModelProvider(),
            session_manager=sm,
            initial_messages=[user("please-never-see-me")],
        )
    )
    result = await agent.run("next question")
    contents = [m.content for m in result.messages]
    assert "persisted question" in contents and "persisted answer" in contents
    assert "please-never-see-me" not in contents  # session wins over the seed
    assert agent.state.get("theme") == "dark"  # state restored on first run

    # save-after-run persists messages + state under the same session
    latest = await sm.load_latest()
    assert latest is not None
    assert "next question" in [m.content for m in (latest.messages or [])]
    assert latest.state == {"theme": "dark"}
    assert sm.session_id == "sess-1"
    assert agent.session_id == "sess-1"


async def test_session_restores_history_into_a_restarted_agent():
    storage = InMemoryStorage()
    sm = SessionManager(storage, session_id="sess-1")
    first = Agent(AgentOptions(model=MockModelProvider(), session_manager=sm))
    await first.run("first ever")
    second = Agent(AgentOptions(model=MockModelProvider(), session_manager=sm))
    result = await second.run("second ever")
    contents = [m.content for m in result.messages]
    assert "first ever" in contents and "mock-response-1" in contents
    assert "second ever" in contents


# ---------------------------------------------------------------------------
# pause / resume / checkpointed HITL
# ---------------------------------------------------------------------------


async def test_pause_then_resume_continues_without_new_input(tmp_path):
    checkpointer = FileCheckpointer(str(tmp_path))
    provider = RecordingProvider()
    agent = Agent(AgentOptions(model=provider))
    first = await agent.run("first")
    assert first.text == "reply-1"

    info = await agent.pause("thread-1", checkpointer)
    assert info == {"thread_id": "thread-1", "messages": len(agent.messages)}

    resumed = await agent.resume("thread-1", checkpointer=checkpointer)
    assert resumed.text == "reply-2"
    assert "resume" in [e["kind"] for e in agent.timeline]
    # continuation re-runs from restored history WITHOUT a new user message
    assert [m.content for m in provider.seen[-1]] == [
        agent.options.system_prompt,
        "first",
        "reply-1",
    ]
    assert resumed.invocation_state is not None


async def test_resume_missing_checkpoint_is_loud(tmp_path):
    agent = Agent(AgentOptions(model=MockModelProvider()))
    with pytest.raises(AgentError) as err:
        await agent.resume("ghost", checkpointer=FileCheckpointer(str(tmp_path)))
    assert err.value.code == "CHECKPOINT_MISSING"
    assert err.value.retryable is False


async def test_resume_requires_checkpointer():
    agent = Agent(AgentOptions(model=MockModelProvider()))
    with pytest.raises(ValueError):
        await agent.resume("t")


async def test_hitl_checkpoint_saved_on_interrupt_then_resume(tmp_path):
    checkpointer = FileCheckpointer(str(tmp_path))
    provider = InterruptingProvider([{"text": "prod"}, {"text": "prod"}])
    agent = Agent(
        AgentOptions(
            model=provider,
            tools=[_dangerous_tool()],
            middleware=[human_in_the_loop({"dangerous": True})],
        )
    )
    state: dict[str, Any] = {}
    with pytest.raises(HitlInterrupt):
        await agent.run("deploy", invocation_state=state, checkpointer=checkpointer, thread_id="t1")

    # checkpoint cut-search: the assistant turn holding the tool_call was
    # dropped; state + the pending call were saved.
    saved = await checkpointer.load("t1")
    assert saved is not None
    assert [m.role for m in saved.messages] == ["system", "user"]
    assert len(saved.pending) == 1
    assert saved.pending[0].tool == "dangerous"
    assert saved.pending[0].args == {"text": "prod"}

    result = await agent.resume(
        "t1", decisions={hitl_key("dangerous", {"text": "prod"}): "approve"}, checkpointer=checkpointer
    )
    assert result.text == "all done"
    assert "resume" in [e["kind"] for e in agent.timeline]


# ---------------------------------------------------------------------------
# v1 output + collect_stream
# ---------------------------------------------------------------------------


async def test_v1_output_stores_reasoning_blocks_on_final_turns():
    def make(version: str) -> Agent:
        class _Think:
            id = "think"
            capabilities = {"structured_output": False, "streaming": False}

            async def complete(self, options: CompleteOptions) -> ModelCompletion:
                return ModelCompletion(
                    text="42",
                    thinking="because math",
                    usage=TokenUsage(input_tokens=1, output_tokens=2),
                )

        return Agent(
            AgentOptions(
                model=_Think(),
                output_version=version,  # type: ignore[arg-type]
            )
        )

    v0 = await make("v0").run("q")
    assert isinstance(v0.messages[-1].content, str)  # plain text (v0 default)
    v1 = await make("v1").run("q")
    blocks = v1.messages[-1].content
    assert not isinstance(blocks, str)
    assert [b["type"] for b in blocks] == ["reasoning", "text"]
    assert blocks[0] == {"type": "reasoning", "reasoning": "because math"}
    assert blocks[1] == {"type": "text", "text": "42"}


async def test_collect_stream_buckets_events_and_result():
    agent = Agent(AgentOptions(model=MockModelProvider()))
    bucket = await collect_stream(agent.stream("hello"))
    assert bucket["result"].text == "mock-response-1"
    kinds = [e["type"] for e in bucket["events"]]
    assert kinds[0] == "step" and kinds[-1] == "done"
    assert "token" in kinds
    assert bucket["result"].messages[-1].content == "mock-response-1"


async def test_collect_stream_raises_without_a_result():
    async def _bare():
        return
        yield  # pragma: no cover - make it a generator

    with pytest.raises(AgentError) as err:
        await collect_stream(_bare())
    assert err.value.code == "RUN_INCOMPLETE"


# ---------------------------------------------------------------------------
# capabilities
# ---------------------------------------------------------------------------


async def test_supports_vision_table():
    assert supports_vision("Qwen/Qwen2.5-VL-72B-Instruct")
    assert supports_vision("llava-1.6-34b")
    assert supports_vision("gpt-4o-mini")
    assert supports_vision("claude-sonnet-4")
    assert supports_vision("gemini-2.0-flash")
    assert supports_vision("llama-3.2-11b-vision")
    assert not supports_vision("meta-llama/Llama-3.2-3B-Instruct")
    assert not supports_vision("mistral-7b-instruct")
    with pytest.raises(AgentError) as err:
        assert_can_handle("mistral-7b-instruct", True)
    assert err.value.code == "MODEL_CAPABILITY"
    assert err.value.retryable is False
    assert_can_handle("mistral-7b-instruct", False)  # no vision needed -> ok
    assert_can_handle("gpt-4o", True)  # vision model -> ok


def _no_network() -> Any:
    raise AssertionError("fail-fast must fire BEFORE any fetch")


async def test_openai_compatible_fail_fast_on_images():
    provider = OpenAICompatibleProvider(
        OpenAICompatibleOptions(
            base_url="http://x",
            api_key="k",
            model="meta-llama/Llama-3.2-3B-Instruct",
            fetch_fn=_no_network,
        )
    )
    image_message = ChatMessage(role="user", content=[{"type": "image", "url": "http://x/y.png"}])
    with pytest.raises(AgentError) as err:
        await provider.complete(CompleteOptions(messages=[image_message]))
    assert err.value.code == "MODEL_CAPABILITY"


async def test_openai_compatible_vision_model_passes():
    provider = OpenAICompatibleProvider(
        OpenAICompatibleOptions(
            base_url="http://x",
            api_key="k",
            model="gpt-4o-mini",
            fetch_fn=lambda: _FakeClient(_FakeOpenAIResponse()),
        )
    )
    image_message = ChatMessage(role="user", content=[{"type": "image", "url": "http://x/y.png"}])
    completion = await provider.complete(CompleteOptions(messages=[image_message]))
    assert completion.text == "ok"


async def test_anthropic_fail_fast_on_images():
    provider = AnthropicProvider(
        AnthropicOptions(
            api_key="k",
            model="claude-3-5-haiku-20241022",
            fetch_fn=_no_network,
        )
    )
    image_message = ChatMessage(role="user", content=[{"type": "image", "url": "http://x/y.png"}])
    with pytest.raises(AgentError) as err:
        await provider.complete(CompleteOptions(messages=[image_message]))
    assert err.value.code == "MODEL_CAPABILITY"


async def test_anthropic_vision_model_passes():
    provider = AnthropicProvider(
        AnthropicOptions(
            api_key="k",
            model="claude-3-7-sonnet",
            fetch_fn=lambda: _FakeClient(_FakeAnthropicResponse()),
        )
    )
    image_message = ChatMessage(role="user", content=[{"type": "image", "url": "http://x/y.png"}])
    completion = await provider.complete(CompleteOptions(messages=[image_message]))
    assert completion.text == "ok"


# ---------------------------------------------------------------------------
# a2a_as_tool
# ---------------------------------------------------------------------------


class _StubA2AClient:
    """Duck-typed A2AClient: send() returns {text, contextId}."""

    endpoint = "http://stub.example/a2a"

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def send(self, text: str, context_id: str | None = None) -> dict[str, Any]:
        self.calls.append((text, context_id))
        return {"text": f"remote:{text}", "contextId": context_id or "ctx-1"}


async def test_a2a_as_tool_calls_through_the_client():
    stub = _StubA2AClient()
    tool = a2a_as_tool(stub)
    assert tool.name == "remote_agent"
    assert "stub.example" in tool.description
    out = await tool.execute(tool.schema.model_validate({"input": "hello"}), None)
    assert out == "remote:hello"
    assert stub.calls == [("hello", None)]

    named = a2a_as_tool(stub, name="agent_b", description="the remote one")
    assert named.name == "agent_b" and named.description == "the remote one"
    out = await named.execute(named.schema.model_validate({"input": "again", "context_id": "ctx-9"}), None)
    assert out == "remote:again"
    assert stub.calls[-1] == ("again", "ctx-9")


async def test_a2a_as_tool_with_real_client_class():
    # the helper also accepts the real A2AClient (schema built the same way)
    client = A2AClient("http://stub.example/a2a/")
    tool = a2a_as_tool(client)
    assert tool.schema.model_validate({"input": "x", "context_id": None}).input == "x"
    assert tool.schema.model_validate({"input": "x"}).context_id is None


# ---------------------------------------------------------------------------
# ollama + provider_from_env
# ---------------------------------------------------------------------------


async def test_ollama_provider_shape():
    provider = ollama("qwen2.5:7b")
    assert isinstance(provider, OllamaProvider)
    assert provider.id == "ollama:qwen2.5:7b"
    assert provider.opts.base_url == "http://localhost:11434/v1"
    assert provider.opts.api_key == "ollama"
    assert provider.capabilities["structured_output"] is False
    assert provider.capabilities["streaming"] is True
    assert isinstance(OllamaOptions(model="m"), OllamaOptions)

    custom = ollama("llama3.1", base_url="http://gpu.box:11434/v1", temperature=0.5, stream=True)
    assert custom.opts.base_url == "http://gpu.box:11434/v1"
    assert custom.opts.temperature == 0.5
    assert custom.opts.stream is True
    assert custom.id == "ollama:llama3.1"


async def test_provider_from_env_shape():
    provider = provider_from_env(
        {"baseUrl": "https://api.acme.test/v1", "apiKey": "sk-1", "model": "Qwen/Qwen2.5-72B-Instruct"}
    )
    assert isinstance(provider, OpenAICompatibleProvider)
    assert provider.id == "openai-compatible:Qwen/Qwen2.5-72B-Instruct"
    assert provider.opts.base_url == "https://api.acme.test/v1"
    assert provider.opts.api_key == "sk-1"
    assert provider.opts.model == "Qwen/Qwen2.5-72B-Instruct"

    extra = provider_from_env(
        {"baseUrl": "https://api.acme.test/v1", "apiKey": "sk-1", "model": "m"},
        {"temperature": 0.9, "max_tokens": 512},
    )
    assert extra.opts.temperature == 0.9
    assert extra.opts.max_tokens == 512

    with pytest.raises(ValueError):
        provider_from_env({"baseUrl": "https://api.acme.test/v1", "model": "m"})
# ---------------------------------------------------------------------------
# run_sync / stream_sync (notebook + script ergonomics)
# ---------------------------------------------------------------------------

class _OnceStub:
    def __init__(self, text: str) -> None:
        self.text = text

    id = "stub"
    capabilities = {"structured_output": False, "streaming": False}

    async def complete(self, options: CompleteOptions) -> ModelCompletion:
        return ModelCompletion(
            text=self.text,
            tool_calls=[],
            usage=TokenUsage(input_tokens=1, output_tokens=1),
        )


def test_run_sync_script_context():
    from framework_agents import Agent, AgentOptions

    agent = Agent(AgentOptions(model=_OnceStub("sync-ok")))
    result = agent.run_sync("hi")
    assert result.text == "sync-ok"


def test_run_sync_inside_running_loop_notebook_context():
    """The Jupyter pattern: run_sync is CALLED while a loop is running. It
    must delegate to a private loop on a worker instead of raising."""

    async def notebook_cell() -> AgentResult:
        agent = Agent(AgentOptions(model=_OnceStub("notebook-ok")))
        # This coroutine runs ON the loop — exactly what a notebook cell is.
        return agent.run_sync("hi")

    result = asyncio.run(notebook_cell())
    assert result.text == "notebook-ok"


def test_stream_sync_iteration_script_and_running_loop():
    from framework_agents import Agent, AgentOptions

    agent1 = Agent(AgentOptions(model=_OnceStub("e1")))
    kinds1 = [e["type"] for e in agent1.stream_sync("hi")]
    assert "done" in kinds1

    async def notebook_cell() -> list[str]:
        agent2 = Agent(AgentOptions(model=_OnceStub("e2")))
        # Called ON a running loop (notebook): stream_sync must still work.
        return [e["type"] for e in agent2.stream_sync("hi")]

    kinds2 = asyncio.run(notebook_cell())
    assert "done" in kinds2
