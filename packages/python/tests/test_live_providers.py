"""Live provider parity — OpenAI + Anthropic, per-provider gated.

Add OPENAI_API_KEY / ANTHROPIC_API_KEY to framework/.env and run:
    E2E_MODEL=1 .venv/bin/python -m pytest tests/test_live_providers.py -q
Without a key the suite SKIPS that provider's scenarios cleanly, so the
offline suite stays hermetic.

Costs are kept tiny: short prompts, bounded max_tokens (Anthropic is billed
per-token at premium rates — scenarios are one-liners).
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys

import pytest
from pydantic import BaseModel

sys.path.insert(0, str(pathlib.Path(__file__).parents[1] / "src"))
_ROOT_ENV = pathlib.Path(__file__).parents[3] / ".env"
if _ROOT_ENV.exists():
    for line in _ROOT_ENV.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))

LIVE = os.environ.get("E2E_MODEL") == "1"

from framework_agents import (  # noqa: E402
    Agent,
    AgentOptions,
    TokenBudgets,
    define_tool,
    provider_strategy,
)
from framework_agents.config import load_config  # noqa: E402
from framework_agents.models.anthropic import AnthropicOptions, AnthropicProvider  # noqa: E402
from framework_agents.models.openai_compatible import OpenAICompatibleOptions, OpenAICompatibleProvider  # noqa: E402

pytestmark = pytest.mark.live

CFG = load_config()


def _needs(key: str):
    return pytest.mark.skipif(not LIVE or not key, reason=f"set E2E_MODEL=1 + {key.split('_')[0]}_API_KEY for this provider")


# --------------------------------------------------------------------------
# OpenAI (gpt-4o-mini through the OpenAI-compatible provider)
# --------------------------------------------------------------------------

@_needs(CFG.openai_api_key)
class TestOpenAILive:
    @pytest.fixture
    def model(self):
        return OpenAICompatibleProvider(OpenAICompatibleOptions(
            base_url=CFG.openai_base_url, api_key=CFG.openai_api_key,
            model=CFG.openai_model, max_tokens=800,
        ))

    @pytest.mark.asyncio
    async def test_usage_and_cost(self, model):
        res = await Agent(AgentOptions(model=model)).run("Reply with exactly: ok")
        assert res.text.strip()
        assert res.usage.input_tokens > 0
        assert res.usage.cost_usd is not None and res.usage.cost_usd > 0  # gpt-4o-mini is table-priced

    @pytest.mark.asyncio
    async def test_tool_parameters(self, model):
        seen: list[str] = []

        class Weather(BaseModel):
            city: str

        async def weather(args: Weather, ctx) -> str:
            seen.append(args.city)
            return "21 celsius"

        res = await Agent(AgentOptions(
            model=model, max_steps=4,
            tools=[define_tool(name="get_weather", description="Get the weather for a city.", schema=Weather, execute=weather)],
        )).run("Call the get_weather tool for Paris. Then state the temperature.")
        assert seen, "regression: model cannot pass args without parameters on the wire"
        assert "paris" in seen[0].lower()

    @pytest.mark.asyncio
    async def test_structured(self, model):
        class Calc(BaseModel):
            answer: int

        res = await Agent(AgentOptions(
            model=model, max_steps=4,
            response_format=provider_strategy(Calc),
        )).run('What is 6 times 7? Answer as {"answer": <number>}.')
        assert res.structured_response is not None
        assert res.structured_response.answer == 42

    @pytest.mark.asyncio
    async def test_streaming_usage(self):
        stream_provider = OpenAICompatibleProvider(OpenAICompatibleOptions(
            base_url=CFG.openai_base_url, api_key=CFG.openai_api_key,
            model=CFG.openai_model, max_tokens=800, stream=True,
        ))
        agent = Agent(AgentOptions(model=stream_provider))
        async for _ in agent.stream("Reply with exactly: ok"):
            pass
        assert agent._last_result.usage.input_tokens > 0  # the old bug: streaming zeros

    @pytest.mark.asyncio
    async def test_budget_fires(self, model):
        with pytest.raises(Exception) as err:
            await Agent(AgentOptions(model=model, budgets=TokenBudgets(max_tokens=1))).run("Say anything")
        assert getattr(err.value, "code", "") in ("BUDGET_EXCEEDED", "MODEL_ERROR")


# --------------------------------------------------------------------------
# Anthropic (native Messages API through the dedicated provider)
# --------------------------------------------------------------------------

@_needs(CFG.anthropic_api_key)
class TestAnthropicLive:
    @pytest.fixture
    def model(self):
        return AnthropicProvider(AnthropicOptions(
            api_key=CFG.anthropic_api_key,
            model=CFG.anthropic_model,
            base_url=CFG.anthropic_base_url,
            max_tokens=512,  # tight — premium model, keep it cheap
        ))

    @pytest.mark.asyncio
    async def test_usage_and_native_tool_call(self, model):
        seen: list[str] = []

        class Weather(BaseModel):
            city: str

        async def weather(args: Weather, ctx) -> str:
            seen.append(args.city)
            return "18 celsius"

        res = await Agent(AgentOptions(
            model=model, max_steps=4,
            tools=[define_tool(name="get_weather", description="Get the weather for a city.", schema=Weather, execute=weather)],
        )).run("Use the get_weather tool for Cairo, then tell me the temperature it returned.")
        assert seen, "the input_schema parameters must reach the model as real args"
        assert "cairo" in seen[0].lower()
        assert res.usage.input_tokens > 0
        # Anthropic usage: cost from the table (claude-sonnet is listed).
        assert res.usage.cost_usd is not None

    @pytest.mark.asyncio
    async def test_streaming_usage(self):
        stream_model = AnthropicProvider(AnthropicOptions(
            api_key=CFG.anthropic_api_key, model=CFG.anthropic_model,
            base_url=CFG.anthropic_base_url, max_tokens=256, stream=True,
        ))
        agent = Agent(AgentOptions(model=stream_model))
        async for _ in agent.stream("Reply with exactly: ok"):
            pass
        # message_start/message_delta usage must land in the completion.
        assert agent._last_result.usage.input_tokens > 0
        assert agent._last_result.usage.output_tokens > 0

    @pytest.mark.asyncio
    async def test_reasoning_subset_invariant(self, model):
        """Anthropic bills thinking inside output_tokens: cost math must
        never ADD reasoning on top (subset semantics)."""
        res = await Agent(AgentOptions(model=model)).run("Reply with exactly: ok")
        if res.usage.reasoning_tokens is not None:
            assert res.usage.reasoning_tokens <= res.usage.output_tokens
        assert res.usage.output_tokens > 0