"""Core parity tests: agent loop, budgets, structured, memory, provider.

Each pinned behavior is a regression the TS sweep found and fixed; the python
port must start from the FIXED version.
"""

from __future__ import annotations

import json
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
    MockModelProvider,
    OpenAICompatibleOptions,
    OpenAICompatibleProvider,
    TokenBudgets,
    define_tool,
    provider_strategy,
)
from framework_agents.models import (  # noqa: E402
    CompleteOptions,
    ModelCompletion,
    ScriptedModelProvider,
    ToolCallRequest,
    ToolWire,
)
from framework_agents.pricing import price_for, usage_cost  # noqa: E402
from framework_agents.types import ChatMessage, TokenUsage  # noqa: E402


class Weather(BaseModel):
    city: str


class CalcAnswer(BaseModel):
    answer: int


def _hungry_model() -> ScriptedModelProvider:
    class _Hungry(ScriptedModelProvider):
        async def complete(self, options: CompleteOptions) -> ModelCompletion:
            return ModelCompletion(
                text="x",
                tool_calls=[],
                usage=TokenUsage(input_tokens=10, output_tokens=10, reasoning_tokens=8),
            )

    return _Hungry([])


async def test_usage_and_cost_never_guessed():
    """Mock model id has no table price: cost_usd stays None — never a fake zero."""
    result = await Agent(AgentOptions(model=MockModelProvider())).run("hi")
    assert result.text
    assert result.usage.input_tokens == 10
    assert result.usage.cost_usd is None


async def test_budget_caps_tokens_without_double_counting_reasoning():
    """reasoning is a SUBSET of output_tokens: max_tokens=15 fires on 10+10."""
    agent = Agent(AgentOptions(model=_hungry_model(), budgets=TokenBudgets(max_tokens=15)))
    with pytest.raises(AgentError) as err:
        await agent.run("q")
    assert err.value.code == "BUDGET_EXCEEDED"
    # A naive total (10+10+8=28) would double-count; subset semantics says
    # 20 real tokens, so a 25 cap must NOT fire.
    ok = await Agent(AgentOptions(model=_hungry_model(), budgets=TokenBudgets(max_tokens=25))).run("q")
    assert ok.text == "x"


async def test_budget_no_price_is_loud():
    class NoPrice(ScriptedModelProvider):
        async def complete(self, options: CompleteOptions) -> ModelCompletion:
            return ModelCompletion(text="ok", usage=TokenUsage(input_tokens=5, output_tokens=5))

    agent = Agent(AgentOptions(model=NoPrice([]), budgets=TokenBudgets(max_cost_usd=1.0)))
    with pytest.raises(AgentError) as err:
        await agent.run("q")
    assert err.value.code == "BUDGET_NO_PRICE"


async def test_memory_clear_then_save_no_duplication():
    """save() APPENDS: the loop must clear first or history doubles each run."""
    memory = InMemoryMemory()
    agent = Agent(AgentOptions(model=MockModelProvider(), memory=memory))
    await agent.run("first")
    await agent.run("second")
    contents = [m.content for m in await memory.load()]
    assert contents == ["first", "mock-response-1", "second", "mock-response-2"]


async def test_unknown_tool_is_feedback_not_crash():
    class CallsUnknownTool(ScriptedModelProvider):
        def __init__(self) -> None:
            super().__init__([])
            self.n = 0

        async def complete(self, options: CompleteOptions) -> ModelCompletion:
            self.n += 1
            if self.n == 1:
                return ModelCompletion(text="", tool_calls=[ToolCallRequest(id="c1", name="nope", args={})])
            return ModelCompletion(text="recovered")

    agent = Agent(AgentOptions(model=CallsUnknownTool(), max_steps=3))
    res = await agent.run("go")
    assert res.text == "recovered"
    assert any(m.content == "Unknown tool: nope" for m in res.messages)


async def test_structured_auto_resolves_by_capability():
    """Unresolved 'auto' made every run throw STRUCTURED_NO_MATCH once."""

    class StructCap(MockModelProvider):
        capabilities = {"structured_output": True, "streaming": False}

        async def complete(self, options: CompleteOptions) -> ModelCompletion:
            return ModelCompletion(text='{"answer": 42}', usage=TokenUsage(input_tokens=1, output_tokens=1))

    agent = Agent(AgentOptions(model=StructCap(), response_format=provider_strategy(CalcAnswer)))
    res = await agent.run("6*7?")
    assert res.structured_response is not None
    assert res.structured_response.answer == 42


def _mock_fetch(handler):
    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _ok(openai_shape: bool) -> httpx.Response:
    """One canned 200 shaped like Together (default) or OpenAI."""
    if openai_shape:
        message = {"content": "ok", "reasoning_content": "hmm"}
        usage = {"prompt_tokens": 2, "completion_tokens": 3, "completion_tokens_details": {"reasoning_tokens": 2}}
    else:
        message = {"content": "391", "reasoning": "Thinking Process: ..."}
        usage = {"prompt_tokens": 10, "completion_tokens": 50, "reasoning_tokens": 40}
    return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": "stop"}], "usage": usage})


async def test_tool_parameters_reach_the_wire():
    """Without `parameters` the API advertises a zero-arg tool (pinned TS fix)."""
    seen: dict = {}

    def transport(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.read())
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5, "reasoning_tokens": 0},
        })

    provider = OpenAICompatibleProvider(OpenAICompatibleOptions(
        base_url="https://api.test/v1", api_key="k", model="m", fetch_fn=_mock_fetch(transport),
    ))
    await provider.complete(CompleteOptions(
        messages=[ChatMessage(role="user", content="hi")],
        tools=[ToolWire(name="get_weather", description="w", parameters_schema=Weather.model_json_schema())],
    ))
    fn = seen["body"]["tools"][0]["function"]
    assert fn["parameters"]["properties"]["city"]["type"] == "string"


async def test_together_and_openai_field_unions():
    """Both provider families' reasoning fields parse (live-confirmed shapes)."""
    together = OpenAICompatibleProvider(OpenAICompatibleOptions(
        base_url="https://api.test/v1", api_key="k", model="m", fetch_fn=_mock_fetch(lambda r: _ok(False)),
    ))
    thoughts: list[str] = []
    out = await together.complete(CompleteOptions(messages=[], on_thinking=thoughts.append))
    assert thoughts
    assert out.thinking == "Thinking Process: ..."
    assert out.usage.reasoning_tokens == 40
    assert out.finish_reason == "stop"

    openai = OpenAICompatibleProvider(OpenAICompatibleOptions(
        base_url="https://api.test/v1", api_key="k", model="m", fetch_fn=_mock_fetch(lambda r: _ok(True)),
    ))
    out2 = await openai.complete(CompleteOptions(messages=[]))
    assert out2.thinking == "hmm"
    assert out2.usage.reasoning_tokens == 2


async def test_truncation_guard_loud_on_empty_content():
    """finish_reason=length with ZERO content raises, never silent empty."""

    def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": ""}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 300, "reasoning_tokens": 0},
        })

    provider = OpenAICompatibleProvider(OpenAICompatibleOptions(
        base_url="https://api.test/v1", api_key="k", model="m", fetch_fn=_mock_fetch(transport),
    ))
    with pytest.raises(AgentError) as err:
        await provider.complete(CompleteOptions(messages=[]))
    assert err.value.code == "MODEL_ERROR"
    assert err.value.retryable is False
    assert "max_tokens" in str(err.value).lower()


async def test_partial_truncation_keeps_content_with_finish_reason():

    def transport(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "half an an"}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 9},
        })

    provider = OpenAICompatibleProvider(OpenAICompatibleOptions(
        base_url="https://api.test/v1", api_key="k", model="m", fetch_fn=_mock_fetch(transport),
    ))
    out = await provider.complete(CompleteOptions(messages=[]))
    assert out.text == "half an an"
    assert out.finish_reason == "length"


async def test_stream_usage_and_guard():
    """stream_options requested; null-usage chunks skipped; final usage chunk
    parsed; streaming truncation with zero content fails loudly."""
    sse = "\n".join([
        'data: {"choices":[{"delta":{"reasoning":"hmm"}}]}',
        'data: {"choices":[{"delta":{"content":"ok"}}],"usage":null}',
        'data: {"choices":[],"usage":{"prompt_tokens":9,"completion_tokens":4,"reasoning_tokens":0}}',
        'data: [DONE]',
    ])

    def transport(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        assert body["stream_options"] == {"include_usage": True}
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse.encode())

    provider = OpenAICompatibleProvider(OpenAICompatibleOptions(
        base_url="https://api.test/v1", api_key="k", model="m", stream=True, fetch_fn=_mock_fetch(transport),
    ))
    out = await provider.complete(CompleteOptions(messages=[]))
    assert out.text == "ok"
    assert out.thinking == "hmm"
    assert (out.usage.input_tokens, out.usage.output_tokens) == (9, 4)

    empty_sse = "\n".join([
        'data: {"choices":[{"delta":{"reasoning":"hmm"}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"length"}]}',
        'data: [DONE]',
    ])

    def transport2(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=empty_sse.encode())

    provider2 = OpenAICompatibleProvider(OpenAICompatibleOptions(
        base_url="https://api.test/v1", api_key="k", model="m", stream=True, fetch_fn=_mock_fetch(transport2),
    ))
    with pytest.raises(AgentError) as err:
        await provider2.complete(CompleteOptions(messages=[]))
    assert err.value.code == "MODEL_ERROR"


def test_pricing_subset_and_ollama_collision():
    """Reasoning never double-billed; local ollama wins over colliding names."""
    usage = TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000, reasoning_tokens=800_000)
    assert usage_cost(usage, "gpt-4o-mini") == pytest.approx(0.75)  # not 1.08
    assert price_for("ollama:gpt-4o-mini").output_per_million == 0
    assert usage_cost(usage, "unknown-model") is None