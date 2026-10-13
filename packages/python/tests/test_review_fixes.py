"""Regression tests for the adversarial-review fixes.

Each pinned behavior below failed before its fix (each was verified with a
reproducer). One test per fix:

1.  reasoning-only stop guard (openai-compatible + anthropic, json + stream)
2.  per-stream AgentStream.result (no instance-global cross-contamination)
3.  initial_messages seeded into an EMPTY memory (TS iterate parity)
4.  before_model list replacement feeds the model call only (history preserved)
5.  ToolContext wired with AgentState + live invocation_state; registry ctx
6.  ToolRejection from before_tool recorded / after_tool applied / loop continues
7.  max_steps synthesized answer carries the LAST tool output
8.  run_with_budget races timeout vs signal (FIRST_COMPLETED)
9.  AgentState.to_json wraps non-serializable values with key context
10. parse_content_block rejects non-block elements; blocks() memoized
11. dotenv: `export` prefix, unquoted trailing comments, LAST-wins duplicates
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys

import httpx
import pytest
from pydantic import BaseModel

sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "src"))

from framework_agents import (  # noqa: E402
    Agent,
    AgentError,
    AgentOptions,
    InMemoryMemory,
    define_tool,
)
from framework_agents.config import _try_setuptools_free_dotenv, parse_env_file  # noqa: E402
from framework_agents.memory import FileMemory  # noqa: E402
from framework_agents.middleware import Middleware, ModelRequest, ToolRejection  # noqa: E402
from framework_agents.models import (  # noqa: E402
    CompleteOptions,
    ModelCompletion,
    ScriptedModelProvider,
    ToolCallRequest,
)
from framework_agents.models.anthropic import AnthropicOptions, AnthropicProvider  # noqa: E402
from framework_agents.models.openai_compatible import (  # noqa: E402
    OpenAICompatibleOptions,
    OpenAICompatibleProvider,
)
from framework_agents.runtime import Budget, run_with_budget  # noqa: E402
from framework_agents.state import AgentState  # noqa: E402
from framework_agents.tools import ToolContext, ToolRegistry  # noqa: E402
from framework_agents.types import (  # noqa: E402
    ChatMessage,
    TokenUsage,
    assistant,
    parse_content_block,
    text_of,
    user,
)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# Fix 1: reasoning-only stop guard (shared framework gap)
# ---------------------------------------------------------------------------


async def test_reasoning_only_stop_json_raises_retryable_model_error():
    """Empty content + NO tool calls + thinking + finish_reason=stop must be
    a LOUD retryable MODEL_ERROR, not a silent empty answer."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "", "reasoning": "the whole answer is 42"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 50, "reasoning_tokens": 50},
        })

    provider = OpenAICompatibleProvider(OpenAICompatibleOptions(
        base_url="https://api.test/v1", api_key="k", model="m", fetch_fn=lambda: _client(handler),
    ))
    with pytest.raises(AgentError) as err:
        await provider.complete(CompleteOptions(messages=[]))
    assert err.value.code == "MODEL_ERROR"
    assert err.value.retryable is True
    message = str(err.value)
    assert "reasoning" in message  # names the reasoning-channel split
    assert "retry or raise maxTokens" in message


async def test_reasoning_plus_content_on_stop_is_kept():
    """Partial: thinking present AND some text -> the completion stands."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "42", "reasoning": "because math"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 10},
        })

    provider = OpenAICompatibleProvider(OpenAICompatibleOptions(
        base_url="https://api.test/v1", api_key="k", model="m", fetch_fn=lambda: _client(handler),
    ))
    out = await provider.complete(CompleteOptions(messages=[]))
    assert out.text == "42"
    assert out.thinking == "because math"


async def test_reasoning_only_stop_stream_raises_retryable_model_error():
    sse = (
        'data: {"choices":[{"delta":{"reasoning":"thinking the answer"}}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
        "data: [DONE]\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=sse.encode(), headers={"content-type": "text/event-stream"})

    provider = OpenAICompatibleProvider(OpenAICompatibleOptions(
        base_url="https://api.test/v1", api_key="k", model="m", stream=True, fetch_fn=lambda: _client(handler),
    ))
    with pytest.raises(AgentError) as err:
        await provider.complete(CompleteOptions(messages=[]))
    assert err.value.code == "MODEL_ERROR"
    assert err.value.retryable is True
    assert "retry or raise maxTokens" in str(err.value)


async def test_anthropic_reasoning_only_stop_json_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "content": [{"type": "thinking", "thinking": "the whole answer lived here"}],
            "stop_reason": "stop",
            "usage": {"input_tokens": 5, "output_tokens": 40},
        })

    provider = AnthropicProvider(AnthropicOptions(
        api_key="k", model="claude-mock", fetch_fn=lambda: _client(handler),
    ))
    with pytest.raises(AgentError) as err:
        await provider.complete(CompleteOptions(messages=[]))
    assert err.value.code == "MODEL_ERROR"
    assert err.value.retryable is True
    assert "retry or raise max_tokens" in str(err.value)


async def test_anthropic_reasoning_only_stop_stream_raises():
    class _FakeStreamResponse:
        status_code = 200

        def __init__(self, chunks: list[bytes]) -> None:
            self._chunks = chunks

        async def aiter_text(self):
            for chunk in self._chunks:
                yield chunk.decode() if isinstance(chunk, bytes) else chunk

    sse = (
        'event: content_block_start\ndata: {"content_block":{"type":"thinking"}}\n\n'
        'event: content_block_delta\ndata: {"delta":{"thinking":"all in reasoning"}}\n\n'
        'event: message_delta\ndata: {"usage":{"output_tokens":40},"delta":{"stop_reason":"stop"}}\n\n'
    )
    provider = AnthropicProvider(AnthropicOptions(api_key="k", model="claude-mock", stream=True))
    with pytest.raises(AgentError) as err:
        await provider._read_stream(_FakeStreamResponse([sse]), CompleteOptions(messages=[], tools=[]))
    assert err.value.code == "MODEL_ERROR"
    assert err.value.retryable is True


# ---------------------------------------------------------------------------
# Fix 2: per-stream AgentStream.result
# ---------------------------------------------------------------------------


class _EchoReplyModel:
    id = "echo-reply"
    capabilities = {"structured_output": False, "streaming": False}

    async def complete(self, options: CompleteOptions) -> ModelCompletion:
        last = options.messages[-1]
        return ModelCompletion(
            text=f"answer:{last.content}",
            usage=TokenUsage(input_tokens=1, output_tokens=1),
        )


async def _drain(stream) -> str:
    async for _ in stream:
        pass
    assert stream.result is not None
    return stream.result.text


async def test_concurrent_runs_on_one_agent_keep_own_results():
    """One agent, two overlapping runs: each must return its OWN text (the
    old agent-global _last_result cross-contaminated)."""
    agent = Agent(AgentOptions(model=_EchoReplyModel()))
    r1, r2 = await asyncio.gather(agent.run("alpha"), agent.run("beta"))
    assert r1.text == "answer:alpha"
    assert r2.text == "answer:beta"
    # one run() + one stream() concurrently: also isolated
    stream = agent.stream("gamma")
    r4_text = asyncio.ensure_future(_drain(stream))
    r3 = await agent.run("delta")
    assert r3.text == "answer:delta"
    assert await r4_text == "answer:gamma"


async def test_done_break_stream_result_is_this_runs_result():
    """A consumer that breaks AT the done event must still get the result
    (_last_result used to be set after the done yield -> None/stale)."""
    model = ScriptedModelProvider([ModelCompletion(text="stream-a", usage=TokenUsage())])
    agent = Agent(AgentOptions(model=model))
    stream = agent.stream("hello")
    async for event in stream:
        if event["type"] == "done":
            break
    assert stream.result is not None
    assert stream.result.text == "stream-a"


# ---------------------------------------------------------------------------
# Fix 3: initial_messages seeded into EMPTY memory
# ---------------------------------------------------------------------------


async def test_initial_messages_seed_memory():
    """initial_messages + memory configured: previously DROPPED; now they seed
    an empty memory before the first run (session-wins-over-seed preserved)."""
    memory = InMemoryMemory()
    agent = Agent(AgentOptions(model=ScriptedModelProvider([ModelCompletion(text="hi", usage=TokenUsage())]), memory=memory,
                               initial_messages=[user("seed")]))
    assert await memory.load() == []  # nothing before the first run
    await agent.run("go")
    loaded = await memory.load()
    assert loaded[0].role == "user"
    assert loaded[0].content == "seed"  # the seed survived the run
    assert loaded[1].content == "go"


# ---------------------------------------------------------------------------
# Fix 4: before_model replacement feeds the model call only
# ---------------------------------------------------------------------------


async def test_before_model_replacement_does_not_touch_history(tmp_path):
    """A one-shot summarizer must NOT become run history: the loop list stays
    authoritative (silent 30-turn data loss before the fix)."""
    memory = FileMemory(str(tmp_path))
    seeded = [user(f"turn{i}") if i % 2 == 0 else assistant(f"reply{i}") for i in range(6)]
    await memory.save(seeded)

    class _Capture:
        id = "capture"
        capabilities = {"structured_output": False, "streaming": False}

        def __init__(self) -> None:
            self.seen: list[list[str]] = []

        async def complete(self, options: CompleteOptions) -> ModelCompletion:
            self.seen.append([m.content for m in options.messages])
            return ModelCompletion(text="done", usage=TokenUsage(input_tokens=1, output_tokens=1))

    async def shorten_first(req, ctx):
        if ctx.step == 0 and len(req.messages) > 3:
            return ModelRequest(
                messages=[req.messages[0], assistant("[summary]"), *req.messages[-2:]],
                tools=req.tools,
                response_format=req.response_format,
            )
        return None

    model = _Capture()
    agent = Agent(AgentOptions(
        model=model,
        memory=memory,
        middleware=[Middleware("summarize-once", before_model=shorten_first)],
    ))
    result = await agent.run("next")

    # the MODEL saw the shortened request...
    assert len(model.seen[0]) == 4
    assert model.seen[0][1] == "[summary]"
    # ...but history after the run retains EVERY original turn.
    non_system = [m for m in result.messages if m.role != "system"]
    contents = [m.content for m in non_system]
    for original in seeded:
        assert original.content in contents
    assert "next" in contents and "done" in contents
    stored = await memory.load()
    assert [m.content for m in stored] == contents


# ---------------------------------------------------------------------------
# Fix 5: ToolContext state/invocation_state wiring (+ registry ctx)
# ---------------------------------------------------------------------------


class _ProbeArgs(BaseModel):
    text: str = ""


async def test_tool_context_carries_agent_state_and_invocation_state():
    recorded: dict = {}

    async def execute(args, ctx):
        recorded["agent_name"] = ctx.agent_name
        recorded["state_k"] = ctx.state.get("k")
        recorded["invocation_i"] = ctx.invocation_state.get("i")
        return "probed"

    probe = define_tool(name="probe", description="records ctx", schema=_ProbeArgs, execute=execute)
    agent = Agent(AgentOptions(
        model=ScriptedModelProvider([
            ModelCompletion(tool_calls=[ToolCallRequest(id="c1", name="probe", args={})]),
            ModelCompletion(text="ok", usage=TokenUsage()),
        ]),
        tools=[probe],
        state={"k": "v"},
        name="ctx-agent",
    ))
    await agent.run("go", invocation_state={"i": 1})
    assert recorded["agent_name"] == "ctx-agent"
    assert recorded["state_k"] == "v"
    assert recorded["invocation_i"] == 1


async def test_tool_registry_call_accepts_optional_ctx():
    received: dict = {}

    async def execute(args, ctx):
        received["state"] = ctx.state.get("k") if ctx is not None else None
        received["invocation"] = ctx.invocation_state.get("i") if ctx is not None else None
        return "ok"

    probe = define_tool(name="probe", description="records ctx", schema=_ProbeArgs, execute=execute)
    registry = ToolRegistry().register(probe)

    # default None stays None (registry callers may pass one)
    assert await registry.call("probe", {}) == "ok"
    assert received == {"state": None, "invocation": None}

    ctx = ToolContext(agent_name="reg", state=AgentState({"k": "vv"}), invocation_state={"i": 9})
    await registry.call("probe", {}, ctx)
    assert received == {"state": "vv", "invocation": 9}


# ---------------------------------------------------------------------------
# Fix 6: ToolRejection from before_tool — TS flow parity
# ---------------------------------------------------------------------------


async def test_before_tool_rejection_is_recorded_after_tooled_and_continues():
    """Rejected calls now follow the TS flow: recorded in result.toolCalls
    with output=feedback, after_tool applied, to_result content as the tool
    message, and the loop continues."""
    seen_after: list[str] = []

    async def reject(_call, _ctx):
        raise ToolRejection("denied by policy")

    async def after_tool(_call, output, _ctx):
        seen_after.append(str(output))
        return f"after:{output}"

    agent = Agent(AgentOptions(
        model=ScriptedModelProvider([
            ModelCompletion(tool_calls=[ToolCallRequest(id="c1", name="probe", args={})]),
            ModelCompletion(text="recovered", usage=TokenUsage()),
        ]),
        tools=[define_tool(name="probe", description="d", schema=_ProbeArgs,
                           execute=lambda a, c: _noop())],
        middleware=[Middleware("rejector", before_tool=reject, after_tool=after_tool)],
    ))
    result = await agent.run("go")
    assert result.text == "recovered"
    assert seen_after == ["denied by policy"]  # after_tool applied
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "probe"
    assert result.tool_calls[0].output == "after:denied by policy"
    tool_message = [m for m in result.messages if m.role == "tool"][0]
    assert tool_message.tool_call_id == "c1"
    assert tool_message.content == "after:denied by policy"  # to_result content


async def _noop() -> str:
    return "never"


# ---------------------------------------------------------------------------
# Fix 7: max_steps synthesized answer carries the last output
# ---------------------------------------------------------------------------


async def test_max_steps_answer_carries_last_tool_output():
    class _AlwaysCall:
        id = "always-call"
        capabilities = {"structured_output": False, "streaming": False}

        def __init__(self) -> None:
            self._n = 0

        async def complete(self, options: CompleteOptions) -> ModelCompletion:
            self._n += 1
            return ModelCompletion(
                tool_calls=[ToolCallRequest(id=f"c{self._n}", name="echo", args={"text": "hi"})],
                usage=TokenUsage(input_tokens=1, output_tokens=1),
            )

    async def echo(args, ctx):
        return f"echo:{args.text}"

    agent = Agent(AgentOptions(
        model=_AlwaysCall(),
        tools=[define_tool(name="echo", description="d", schema=_ProbeArgs, execute=echo)],
        max_steps=2,
    ))
    result = await agent.run("go")
    assert result.text.startswith("Completed 2 tool call(s). ")
    assert 'Last output: "echo:hi"' in result.text  # TS `Last output: ${JSON.stringify(...)}`
    assert result.messages[-1].role == "assistant"
    assert result.messages[-1].content == result.text


# ---------------------------------------------------------------------------
# Fix 8: run_with_budget races timeout vs signal
# ---------------------------------------------------------------------------


async def test_budget_timeout_fires_when_signal_provided_but_unset():
    """The guard used to wait ONLY on the signal when both were given."""
    unset_signal = asyncio.Event()

    async def slow():
        await asyncio.sleep(2.0)
        return "late"

    with pytest.raises(AgentError) as err:
        await run_with_budget(slow, Budget(signal=unset_signal, timeout_ms=100))
    assert err.value.code == "TIMEOUT"
    assert err.value.retryable is True


async def test_budget_signal_wins_when_set_first():
    signal = asyncio.Event()

    async def slow():
        await asyncio.sleep(2.0)
        return "late"

    async def setter():
        await asyncio.sleep(0.05)
        signal.set()

    guard = asyncio.ensure_future(run_with_budget(slow, Budget(signal=signal, timeout_ms=10_000)))
    asyncio.ensure_future(setter())
    with pytest.raises(AgentError) as err:
        await guard
    assert err.value.code == "ABORTED"


# ---------------------------------------------------------------------------
# Fix 9: AgentState.to_json — key context on nested non-serializable values
# ---------------------------------------------------------------------------


def test_state_to_json_names_the_leaking_key():
    state = AgentState({"cfg": {"nested": {}}})
    injected = state.get("cfg")  # by reference
    injected["nested"]["leak"] = object()  # bypassed set() validation
    with pytest.raises(ValueError) as err:
        state.to_json()
    message = str(err.value)
    assert "plain objects/lists" in message
    assert "'cfg'" in message  # the key context
    assert "JSON-serializable" in message


def test_state_to_json_still_round_trips_clean_values():
    state = AgentState({"theme": "dark", "counts": [1, 2, 3], "nested": {"a": True}})
    assert state.to_json() == {"theme": "dark", "counts": [1, 2, 3], "nested": {"a": True}}


# ---------------------------------------------------------------------------
# Fix 10: content-list element guard + blocks() memoization
# ---------------------------------------------------------------------------


def test_parse_content_block_rejects_non_dict_elements():
    for bad in ("a bare string", 42, ["nested"], None):
        with pytest.raises(ValueError) as err:
            parse_content_block(bad)
        message = str(err.value)
        assert "content list elements must be block dicts/models" in message
        assert type(bad).__name__ in message  # the offending type is named


def test_blocks_and_text_of_reject_bare_string_elements():
    message = ChatMessage(role="user", content=["just a string"])
    with pytest.raises(ValueError):
        message.blocks()
    with pytest.raises(ValueError):
        text_of(message.content)


def test_blocks_parse_is_memoized_per_instance(monkeypatch):
    import framework_agents.types as types_mod

    calls = {"n": 0}
    real = types_mod.parse_content_block

    def counting(raw):
        calls["n"] += 1
        return real(raw)

    monkeypatch.setattr(types_mod, "parse_content_block", counting)
    message = ChatMessage(role="user", content=[
        {"type": "text", "text": "a"},
        {"type": "reasoning", "reasoning": "b"},
    ])
    first = message.blocks()
    second = message.blocks()
    assert [type(b).__name__ for b in first] == ["TextBlock", "ReasoningBlock"]
    assert first[0] is second[0]  # same parsed instances: cached, not re-parsed
    assert calls["n"] == 2  # parsed ONCE for two blocks() calls
    # revalidation clears the cache entry, a NEW instance parses fresh
    fresh = ChatMessage.model_validate(message.model_dump())
    fresh.blocks()
    assert calls["n"] == 4


def test_str_content_blocks_is_empty(monkeypatch):
    message = ChatMessage(role="user", content="plain text")
    assert message.blocks() == []


# ---------------------------------------------------------------------------
# Fix 11: dotenv loader
# ---------------------------------------------------------------------------


def test_dotenv_export_prefix_supported():
    assert parse_env_file("export FOO=bar") == {"FOO": "bar"}
    assert parse_env_file("  export  SPACED=v ") == {"SPACED": "v"}


def test_dotenv_inline_comments_stripped_only_when_unquoted():
    assert parse_env_file("KEY=v # note") == {"KEY": "v"}
    assert parse_env_file("KEY=v # note with more words") == {"KEY": "v"}
    assert parse_env_file("KEY=a#b") == {"KEY": "a#b"}  # hash without a space: value
    # quoted values keep EVERYTHING inside the quotes (comments included)
    assert parse_env_file('KEY="v # keep"') == {"KEY": "v # keep"}
    assert parse_env_file("KEY='v # keep'") == {"KEY": "v # keep"}


def test_dotenv_duplicate_keys_last_wins_within_one_file():
    assert parse_env_file("K=one\nK=two\nK=three\n") == {"K": "three"}


def test_dotenv_file_load_export_and_last_wins(tmp_path, monkeypatch):
    """Through the real loader: `export` stripped, LAST duplicate wins, and
    the real process env still beats file values (setdefault)."""
    key = "FRAMEWORK_REVIEW_FIX_PROBE"
    env_file = tmp_path / ".env"
    env_file.write_text(f"export {key}=first\n{key}=second\n")
    monkeypatch.setenv("FRAMEWORK_ENV_PATH", str(env_file))
    had = key in os.environ
    old = os.environ.get(key)
    try:
        _try_setuptools_free_dotenv()
        assert os.environ.get(key) == "second"  # export stripped + LAST wins
    finally:
        if had:
            os.environ[key] = old  # type: ignore[index]
        else:
            os.environ.pop(key, None)


def test_dotenv_process_env_beats_file_value(tmp_path, monkeypatch):
    key = "FRAMEWORK_REVIEW_FIX_PROBE2"
    env_file = tmp_path / ".env"
    env_file.write_text(f"{key}=from_file\n")
    monkeypatch.setenv("FRAMEWORK_ENV_PATH", str(env_file))
    monkeypatch.setenv(key, "from_process")
    _try_setuptools_free_dotenv()
    assert os.environ[key] == "from_process"


# ---------------------------------------------------------------------------
# Batch 2 (second review round). One regression per numbered fix:
#
# 1.  AgentClientOptions.timeout_ms actually applied (send/get_card/stream)
#     + stream `signal` aborts mid-iteration
# 2.  _HttpRetry aclose()s streamed failed responses before retrying
# 3.  /stream pre-header errors are real HTTP responses (413 / -32603)
# 4.  HEAD = headers only; status-specific reason phrases (500 != "OK")
# 5.  sandbox execute kills the child at max_buffer MID-stream
# 6.  bridged MCP args omit unset optionals; explicit nulls never serialized
# 7.  filesystem read/edit decode errors are feedback, never raw escapes
# 8.  PII custom-detector duplicate matches: every occurrence redacted
# 9.  tool_retry/model_retry pass AgentError + HitlInterrupt through, always
# 10. on_failure callable None/non-str results coerced (never a None content)
# 11. load_jsonl strips a UTF-8 BOM (PowerShell JSONLs)
# 12. swarm member clone: session_manager dropped, state copied not aliased
# 13. --port validation: friendly error + exit 1 (no raw traceback)
# (14 documents the cold-cache double-build race in code comments only.)
# ---------------------------------------------------------------------------

import json  # noqa: E402
import time  # noqa: E402

from framework_agents import a2a_client  # noqa: E402
from framework_agents.a2a_server import A2AServer  # noqa: E402
from framework_agents.evals import load_jsonl  # noqa: E402
from framework_agents.middleware import (  # noqa: E402
    HitlInterrupt,
    HookContext,
    ToolCallRef,
    filesystem_middleware,
    filesystem_tools,
    model_retry,
    pii_redact,
    tool_retry,
)
from framework_agents.mcp_client import mcp_tools_to_framework  # noqa: E402
from framework_agents.sandbox import create_sandbox_backend  # noqa: E402
from framework_agents.serve import main as serve_main  # noqa: E402
from framework_agents.sessions import LocalFileStorage, SessionManager  # noqa: E402
from framework_agents.swarm import HANDOFF_TOOL, Swarm, handoff_signal, read_handoff_signal  # noqa: E402


def _env(text: str, context_id: str | None = None, method: str = "message/send") -> dict:
    params: dict = {"message": {"role": "user", "parts": [{"kind": "text", "text": text}]}}
    if context_id:
        params["contextId"] = context_id
    return {"jsonrpc": "2.0", "id": "t-1", "method": method, "params": params}


def _stub_server(**server_kwargs) -> A2AServer:
    card = server_kwargs.pop("card", {"name": "stub-agent", "description": "stub"})

    def factory(context_id: str):
        return Agent(AgentOptions(model=ScriptedModelProvider([ModelCompletion(text="ok", usage=TokenUsage())]), name="stub-agent"))

    return A2AServer(card, factory, **server_kwargs)


# -- fix 1: timeout_ms + stream abort ---------------------------------------


class _SlowUpstream(httpx.AsyncBaseTransport):
    """Stands in for an endpoint that takes 5s: a read deadline < 5s must
    fire (an httpx transport enforces read timeouts; MockTransport doesn't,
    so the deadline check doubles as the transport-side enforcement)."""

    def __init__(self) -> None:
        self.seen_reads: list[float | None] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        read = (request.extensions.get("timeout") or {}).get("read")
        self.seen_reads.append(read)
        if read is not None and read < 4.0:
            raise httpx.ReadTimeout(f"upstream exceeded the {read}s read deadline")
        await asyncio.sleep(5)  # only reachable with the buggy 5s default
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"contextId": "ctx-1", "message": {"role": "agent", "parts": [{"kind": "text", "text": "late"}]}}})


def _zero_backoff(monkeypatch) -> None:
    """Keep _HttpRetry's exponential wait from dominating test wall time."""
    real_sleep = asyncio.sleep

    async def _fast(delay=None, *args, **kwargs):
        return await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _fast)


async def test_a2a_timeout_ms_reaches_every_request_not_httpx_default(monkeypatch) -> None:
    seen: list[tuple[str, float | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        read = (request.extensions.get("timeout") or {}).get("read")
        seen.append((request.method + request.url.path, read))
        if request.url.path == "/stream":
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=(
                    'data: {"contextId":"ctx-1","event":{"type":"step","step":0}}\n\n'
                    'data: {"contextId":"ctx-1","done":true,"text":"ok"}\n\n'
                    "data: [DONE]\n\n"
                ).encode(),
            )
        if request.method == "GET":
            return httpx.Response(200, json={"name": "stub-agent"})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"contextId": "ctx-1", "message": {"role": "agent", "parts": [{"kind": "text", "text": "ok"}]}}})

    opts = a2a_client.AgentClientOptions(timeout_ms=1234, fetch_fn=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    sender = a2a_client.A2AClient("http://stub", opts)
    await sender.get_card()
    assert await sender.send("hi") == {"text": "ok", "contextId": "ctx-1"}
    events = [event async for event in a2a_client.AgentClient("http://stub", opts).stream("hi")]
    assert events  # frames flowed
    # every wire request carried the CONFIGURED read deadline — httpx's
    # silent 5s default (read=None or 5.0) must never govern.
    assert seen and all(read == 1234 / 1000 for _, read in seen)
    assert {route for route, _ in seen} == {"GET/.well-known/agent-card.json", "POST/", "POST/stream"}


async def test_a2a_short_timeout_is_clean_agent_error_not_silent_5s(monkeypatch) -> None:
    _zero_backoff(monkeypatch)
    slow = _SlowUpstream()
    opts = a2a_client.AgentClientOptions(timeout_ms=50, fetch_fn=lambda: httpx.AsyncClient(transport=slow))
    started = time.monotonic()

    # A2AClient.send: timeout fires as a framework AgentError, fast.
    with pytest.raises(AgentError) as err:
        await a2a_client.A2AClient("http://stub", opts).send("slow endpoint")
    assert err.value.code == "A2A_TRANSPORT"

    # AgentClient.stream: same contract on the streamed path.
    seen_before = len(slow.seen_reads)
    stream = a2a_client.AgentClient("http://stub", opts).stream("slow endpoint")
    with pytest.raises(AgentError) as stream_err:
        async for _ in stream:
            pass
    assert stream_err.value.code == "A2A_TRANSPORT"
    assert slow.seen_reads[seen_before:]  # the streamed attempts saw 0.05 too
    elapsed = time.monotonic() - started
    assert elapsed < 4  # neither call waited on httpx's 5s default
    assert all(read == 0.05 for read in slow.seen_reads)


async def test_a2a_stream_signal_set_mid_iteration_terminates_generator() -> None:
    """The documented abort shape: an asyncio.Event set while consuming
    frames must end the stream — the 10s tail below proves the abort stops
    the pull instead of draining forever."""

    def handler(request: httpx.Request) -> httpx.Response:
        async def sse():
            yield b'data: {"contextId":"ctx-x","event":{"type":"step","step":0}}\n\n'
            yield b'data: {"contextId":"ctx-x","event":{"type":"token","delta":"hi"}}\n\n'
            await asyncio.sleep(10)  # unreachable when the abort works
            yield b'data: {"contextId":"ctx-x","done":true,"text":"late"}\n\ndata: [DONE]\n\n'

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse())

    client = a2a_client.AgentClient(
        "http://stub", a2a_client.AgentClientOptions(fetch_fn=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    )
    stop = asyncio.Event()
    seen: list[dict] = []
    started = time.monotonic()
    async for event in client.stream("hello", signal=stop):
        seen.append(event)
        if len(seen) == 1:
            stop.set()
    elapsed = time.monotonic() - started
    assert [e["type"] for e in seen] == ["step"]  # aborted right after the first event
    assert elapsed < 5  # the 10-second tail was never awaited


# -- fix 2: streamed retried responses are released --------------------------


class _TrackedStream(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.closed = False

    async def __aiter__(self):
        yield b"server exploded"

    async def aclose(self) -> None:
        self.closed = True


async def test_http_retry_closes_streamed_failed_responses_before_retrying(monkeypatch) -> None:
    streams: list[_TrackedStream] = []

    def handler(request: httpx.Request) -> httpx.Response:
        stream = _TrackedStream()
        streams.append(stream)
        return httpx.Response(500, stream=stream)

    _zero_backoff(monkeypatch)
    retry = a2a_client._HttpRetry(a2a_client._RetryPolicy(max_retries=1))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(AgentError) as err:
            await retry.request(client, "POST", "http://retry.test/stream", stream=True, json=_env("x", method="message/stream"))
    assert err.value.code == "A2A_TRANSPORT"
    assert len(streams) == 2  # one attempt per streamed request
    assert all(s.closed for s in streams)  # both released before/after retry — no pool leak


# -- fix 3: /stream pre-header errors are real HTTP responses ----------------


async def test_stream_pre_header_failures_answer_http_never_sse_frames() -> None:
    server = _stub_server(max_body_bytes=64)
    port = await server.start()
    base = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient() as http:
            # Oversized body -> a proper 413 JSON-RPC response.
            res = await http.post(f"{base}/stream", json=_env("way more than sixty four bytes of request body, surely", method="message/stream"))
            assert res.status_code == 413
            assert res.headers["content-type"] == "application/json"
            body = res.json()  # parseable HTTP body (the bug wrote status-less SSE frames)
            assert body["id"] is None
            assert body["error"]["code"] == -32603
            assert body["error"]["message"].startswith("Request body exceeds")

            # Malformed JSON body -> JSON-RPC -32603 over HTTP, not event-stream.
            res2 = await http.post(f"{base}/stream", content=b"{this is not json", headers={"content-type": "application/json"})
            assert res2.status_code == 200
            assert res2.headers["content-type"].startswith("application/json")
            rpc_error = res2.json()
            assert rpc_error["id"] is None
            assert rpc_error["error"]["code"] == -32603
    finally:
        await server.stop()


# -- fix 4: HEAD bodies suppressed + status-specific phrases ----------------


async def _raw_roundtrip(port: int, request_line: str) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(f"{request_line}\r\nHost: t\r\nConnection: close\r\n\r\n".encode())
        await writer.drain()
        data = b""
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                break
            data += chunk
        return data
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


async def test_head_requests_send_headers_only() -> None:
    card = {"name": "head-agent", "description": "stub for HEAD"}
    server = _stub_server(card=card)
    port = await server.start()
    try:
        # HEAD on a GET route: 200 + the would-be body's Content-Length, NO body.
        raw = await _raw_roundtrip(port, "HEAD /health HTTP/1.1")
        head, sep, rest = raw.partition(b"\r\n\r\n")
        assert sep  # header terminator present
        assert head.startswith(b"HTTP/1.1 200 OK\r\n")
        assert b"content-type: application/json" in head
        assert f"content-length: {len(json.dumps({'ok': True}))}".encode() in head
        assert rest == b""  # Node-style suppression: zero body bytes

        # HEAD on the card route: same contract.
        raw_card = await _raw_roundtrip(port, "HEAD /.well-known/agent-card.json HTTP/1.1")
        head_card, _, rest_card = raw_card.partition(b"\r\n\r\n")
        assert head_card.startswith(b"HTTP/1.1 200 OK\r\n")
        assert f"content-length: {len(json.dumps(card))}".encode() in head_card
        assert rest_card == b""

        # HEAD on an unknown route: 404 headers, still no body.
        raw_404 = await _raw_roundtrip(port, "HEAD /nope HTTP/1.1")
        assert raw_404.startswith(b"HTTP/1.1 404 Not Found\r\n")
        assert raw_404.partition(b"\r\n\r\n")[2] == b""
    finally:
        await server.stop()


def test_status_reason_phrase_is_status_specific_500_not_ok() -> None:
    class _Sink:
        def __init__(self) -> None:
            self.data = b""

        def write(self, chunk: bytes) -> None:
            self.data += chunk

    sink = _Sink()
    A2AServer({"name": "s"}, lambda ctx: None)._send(sink, 500, b"boom", {})
    assert sink.data.startswith(b"HTTP/1.1 500 Internal Server Error\r\n")  # was "500 OK"

    unknown = _Sink()
    A2AServer({"name": "s"}, lambda ctx: None)._send(unknown, 599, b"odd", {})
    assert unknown.data.startswith(b"HTTP/1.1 599 Error\r\n")  # unknown 5xx never reads OK


# -- fix 5: sandbox kills at max_buffer mid-stream ---------------------------


async def test_sandbox_output_cap_kills_tight_loop_well_before_timeout(tmp_path) -> None:
    backend = create_sandbox_backend(str(tmp_path), timeout_ms=8000, max_buffer=100_000)
    (execute,) = backend.tools()
    args = execute.schema.model_validate({"command": "yes"})  # prints forever, tightly
    started = time.monotonic()
    with pytest.raises(AgentError) as err:
        await execute.execute(args, None)
    elapsed = time.monotonic() - started
    assert err.value.code == "SANDBOX_OUTPUT_TOO_LARGE"
    assert err.value.retryable is False
    assert elapsed < 5  # killed mid-run, far under the 8s timeout (old code hit the timeout)


# -- fix 6: bridged MCP args omit unset optionals ----------------------------


async def test_bridged_mcp_omits_unset_optionals_and_never_serializes_null() -> None:
    class CaptureMcp:
        server_id = "cap"

        def __init__(self) -> None:
            self.payloads: list[dict] = []

        async def list_tools(self):
            return [
                {
                    "name": "edit_file",
                    "description": "edit a path",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}, "create": {"type": "boolean"}, "parents": {"type": "boolean"}},
                        "required": ["path"],
                    },
                }
            ]

        async def call_tool(self, name: str, args) -> str:
            self.payloads.append(args)
            return "ok"

        async def close(self) -> None:
            return None

    mcp = CaptureMcp()
    (tool,) = await mcp_tools_to_framework(mcp)

    await tool.execute(tool.schema.model_validate({"path": "x.txt"}), None)
    assert mcp.payloads[-1] == {"path": "x.txt"}  # omitted optionals are ABSENT on the wire

    await tool.execute(tool.schema.model_validate({"path": "y.txt", "create": None}), None)
    assert mcp.payloads[-1] == {"path": "y.txt"}  # present-but-null never appears either

    await tool.execute(tool.schema.model_validate({"path": "z.txt", "create": True, "parents": False}), None)
    assert mcp.payloads[-1] == {"path": "z.txt", "create": True, "parents": False}  # set values round-trip


# -- fix 7: filesystem decode errors are tool feedback ------------------------


async def test_filesystem_decode_errors_are_feedback_never_exceptions(tmp_path) -> None:
    (tmp_path / "blob.bin").write_bytes(b"\xff\xfe\x00\x81 not utf-8 anywhere")
    by_name = {t.name: t for t in filesystem_tools(tmp_path)}
    read_args = by_name["read_file"].schema.model_validate({"path": "blob.bin"})
    out = await by_name["read_file"].execute(read_args, None)
    assert isinstance(out, str)  # feedback string, never a raised UnicodeDecodeError
    assert out.startswith("Error:")
    assert "not valid UTF-8" in out
    edit_args = by_name["edit_file"].schema.model_validate({"path": "blob.bin", "old_string": "a", "new_string": "b"})
    edit_out = await by_name["edit_file"].execute(edit_args, None)
    assert isinstance(edit_out, str)
    assert edit_out.startswith("Error:")
    assert "not valid UTF-8" in edit_out


async def test_binary_read_does_not_kill_the_agent_run(tmp_path) -> None:
    (tmp_path / "blob.bin").write_bytes(b"\xff\x00\xfe binary payload")
    agent = Agent(
        AgentOptions(
            model=ScriptedModelProvider(
                [
                    ModelCompletion(tool_calls=[ToolCallRequest(id="c1", name="read_file", args={"path": "blob.bin"})]),
                    ModelCompletion(text="could not read the file", usage=TokenUsage()),
                ]
            ),
            middleware=[filesystem_middleware(tmp_path)],
        )
    )
    result = await agent.run("read it")  # no TOOL_EXECUTION crash out of the loop
    assert result.text == "could not read the file"
    tool_message = [m for m in result.messages if m.role == "tool"][0]
    assert "not valid UTF-8" in tool_message.content


# -- fix 8: PII duplicate-detector matches ------------------------------------


async def _pii_after_output(mw, text: str):
    completion = ModelCompletion(text=text, usage=TokenUsage())
    return await mw.after_model(completion, ModelRequest(messages=[user("x")]), HookContext(step=0))


async def test_pii_duplicate_detector_redacts_every_real_occurrence() -> None:
    content = "a SECRET b SECRET c"
    mw = pii_redact("custom", strategy="redact", detector=lambda c: ["SECRET", "SECRET"])  # same string twice
    scrubbed = await _pii_after_output(mw, content)
    assert scrubbed is not None
    assert scrubbed.text == "a [REDACTED_CUSTOM] b [REDACTED_CUSTOM] c"
    assert "SECRET" not in scrubbed.text  # the SECOND occurrence does not leak
    assert "[REDACTED_[REDACTED" not in scrubbed.text  # ...and no corrupted double patch


async def test_pii_duplicate_detector_extra_dups_dropped_without_corruption() -> None:
    content = "a SECRET b SECRET c"
    mw = pii_redact("custom", strategy="mask", detector=lambda c: ["SECRET"] * 3)  # 3 dups, 2 real occurrences
    scrubbed = await _pii_after_output(mw, content)
    assert scrubbed is not None
    assert scrubbed.text == "a S..... b S..... c"  # exactly the two real ones, extra dropped

    span_mw = pii_redact("custom", strategy="redact", detector=lambda c: [(2, 8), (2, 8)])  # duplicate spans
    span_out = await _pii_after_output(span_mw, content)
    assert span_out is not None
    assert span_out.text == "a [REDACTED_CUSTOM] b SECRET c"  # one span claimed once: distinct, no corruption


# -- fix 9: control errors pass through retry middleware ---------------------


async def test_tool_retry_on_failure_continue_passes_swarm_handoff_through() -> None:
    mw = tool_retry(max_retries=1, on_failure="continue")
    handoff = handoff_signal("helper", "pass it over")

    async def next_fn():
        raise handoff

    with pytest.raises(AgentError) as err:
        await mw.around_tool(ToolCallRef(name=HANDOFF_TOOL), next_fn, HookContext(step=0))
    assert err.value is handoff  # untouched: the very same error object
    assert read_handoff_signal(err.value).to == "helper"


async def test_tool_retry_on_failure_continue_passes_hitl_interrupt_through() -> None:
    mw = tool_retry(max_retries=1, on_failure="continue")
    interrupt = HitlInterrupt("sensitive_tool:{}", "sensitive_tool", {})

    async def next_fn():
        raise interrupt

    with pytest.raises(HitlInterrupt) as err:
        await mw.around_tool(ToolCallRef(name="sensitive_tool"), next_fn, HookContext(step=0))
    assert err.value is interrupt


async def test_swarm_handoff_routes_despite_tool_retry_on_failure_continue() -> None:
    lead_model = ScriptedModelProvider(
        [ModelCompletion(tool_calls=[ToolCallRequest(id="h1", name=HANDOFF_TOOL, args={"to": "helper", "message": "do it"})])]
    )
    helper = Agent(AgentOptions(model=ScriptedModelProvider([ModelCompletion(text="helper done", usage=TokenUsage())]), name="helper"))
    lead = Agent(AgentOptions(model=lead_model, name="lead", middleware=[tool_retry(max_retries=1, on_failure="continue")]))
    result = await Swarm([lead, helper]).run("delegate please")
    assert result.status == "COMPLETED"
    assert result.text == "helper done"  # the handoff ROUTED (bug: degraded to feedback text)
    assert result.node_history == ["lead", "helper"]


async def test_model_retry_on_failure_continue_passes_agent_error_through() -> None:
    mw = model_retry(max_retries=1, on_failure="continue")
    budget = AgentError("BUDGET_EXCEEDED", "over budget", False)

    async def raiser(req):
        raise budget

    with pytest.raises(AgentError) as err:
        await mw.wrap_model(ModelRequest(messages=[]), raiser, HookContext(step=0))
    assert err.value is budget  # control errors reach the caller untouched

    async def transient(req):
        raise RuntimeError("transient hiccup")

    degraded = await mw.wrap_model(ModelRequest(messages=[]), transient, HookContext(step=0))
    assert isinstance(degraded, ModelCompletion)  # genuine unexpected failures still degrade
    assert "Model call failed after retries: transient hiccup" in degraded.text


# -- fix 10: on_failure callables never produce None --------------------------


async def test_tool_retry_on_failure_none_and_non_str_coerced() -> None:
    async def boom():
        raise RuntimeError("x")

    none_mw = tool_retry(max_retries=0, on_failure=lambda last: None)
    out = await none_mw.around_tool(ToolCallRef(name="t"), boom, HookContext(step=0))
    assert out == "Tool failed after retries"  # coerced fallback, never None

    weird_mw = tool_retry(max_retries=0, on_failure=lambda last: {"nested": 1})
    weird = await weird_mw.around_tool(ToolCallRef(name="t"), boom, HookContext(step=0))
    assert isinstance(weird, str)  # non-str return stringified
    assert "nested" in weird


async def test_model_retry_on_failure_none_and_non_str_coerced() -> None:
    async def boom(req):
        raise RuntimeError("x")

    none_mw = model_retry(max_retries=0, on_failure=lambda last: None)
    completion = await none_mw.wrap_model(ModelRequest(messages=[]), boom, HookContext(step=0))
    assert completion.text == "Model call failed after retries"  # never a None text

    weird_mw = model_retry(max_retries=0, on_failure=lambda last: 42)
    weird = await weird_mw.wrap_model(ModelRequest(messages=[]), boom, HookContext(step=0))
    assert weird.text == "42"


# -- fix 11: BOM-prefixed JSONL loads -----------------------------------------


def test_load_jsonl_strips_utf8_bom(tmp_path) -> None:
    path = tmp_path / "cases.jsonl"
    # PowerShell-written JSONL: UTF-8 BOM before line 1 (line 1 crashed without
    # the strip).
    path.write_bytes(
        b'\xef\xbb\xbf{"id": "c1", "input": "hello", "expect": {"contains": ["hi"]}}\n'
        b'{"id": "c2", "input": "goodbye"}\n'
    )
    cases = load_jsonl(path)
    assert [c.id for c in cases] == ["c1", "c2"]
    assert cases[0].expect is not None and cases[0].expect.contains == ["hi"]
    assert cases[1].expect is not None


# -- fix 12: swarm member clones stay isolated ---------------------------------


async def test_swarm_member_clone_drops_session_manager_and_copies_state(tmp_path) -> None:
    parent = Agent(
        AgentOptions(
            model=ScriptedModelProvider([ModelCompletion(text="ok", usage=TokenUsage())]),
            name="lead",
            state={"role": "lead"},
            session_manager=SessionManager(storage=LocalFileStorage(str(tmp_path / "sessions")), session_id="lead:s1"),
        )
    )
    swarm = Swarm([parent])
    member = swarm._member_with_handoff(parent, {})
    assert member is not parent
    assert member.options.session_manager is None  # a shared manager would cross-pollinate histories
    assert member.options.memory is None
    assert member.state is not parent.state  # copied, never aliased
    assert member.state.to_json() == {"role": "lead"}
    member.state.set("role", "mutated")
    member.state.set("extra", "grown mid-handoff")
    assert parent.state.to_json() == {"role": "lead"}  # no write-back into the original


# -- fix 13: --port validation is a friendly error -------------------------------


def test_cli_invalid_port_is_friendly_error_and_exit_1(tmp_path, capsys) -> None:
    config = tmp_path / "framework.json"
    config.write_text(json.dumps({"server": {"host": "127.0.0.1", "basePort": 0}, "agents": [{"name": "a", "model": "stub:model"}]}), encoding="utf-8")
    code = serve_main(["dev", "--port", "-1", "-c", str(config)])
    assert code == 1
    err = capsys.readouterr().err
    assert "[framework] Invalid --port" in err  # friendly line, not a pydantic traceback
    assert "usage:" not in err