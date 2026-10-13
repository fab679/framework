"""Live parity tests — the same scenarios as the TS e2e suite, from Python.

Gate (identical to TS): TOGETHER_API_KEY in root .env + E2E_MODEL=1.
    cd packages/python && E2E_MODEL=1 .venv/bin/python -m pytest tests/test_agent_live_together.py -q
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

LIVE = os.environ.get("E2E_MODEL") == "1" and bool(os.environ.get("TOGETHER_API_KEY"))

from framework_agents import (  # noqa: E402
    Agent,
    AgentError,
    AgentOptions,
    OpenAICompatibleOptions,
    OpenAICompatibleProvider,
    TokenBudgets,
    define_tool,
    provider_strategy,
)
from framework_agents.config import load_config  # noqa: E402

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not LIVE, reason="set E2E_MODEL=1 + TOGETHER_API_KEY for live parity tests"),
]


def live_model(max_tokens: int = 1200) -> OpenAICompatibleProvider:
    cfg = load_config()
    assert cfg.together_api_key, "set TOGETHER_API_KEY in framework/.env"
    return OpenAICompatibleProvider(OpenAICompatibleOptions(
        base_url=cfg.together_base_url,
        api_key=cfg.together_api_key,
        model=cfg.together_model,
        max_tokens=max_tokens,
    ))


class Calc(BaseModel):
    answer: int


async def test_live_usage_and_cost():
    res = await Agent(AgentOptions(model=live_model())).run("Reply with exactly: ok")
    assert res.text.strip()
    assert res.usage.input_tokens > 0
    assert res.usage.output_tokens > 0
    # Qwen/Qwen3.5-9B is table-priced: cost_usd must exist and match the math.
    assert res.usage.cost_usd is not None and res.usage.cost_usd > 0


async def test_live_streaming_usage_and_thinking():
    agent = Agent(AgentOptions(model=live_model(max_tokens=2500)))
    text = ""
    async for event in agent.stream("Think briefly, then answer: what is 6 times 7? Reply with only the number."):
        if event["type"] == "done":
            text = event["text"]
    assert agent._last_result is not None
    usage = agent._last_result.usage
    assert usage.input_tokens > 0, "streaming usage must be real (the old bug: all zeros)"
    assert usage.output_tokens > 0
    assert "42" in text


async def test_live_tool_parameters():
    seen: list[str] = []

    class Weather(BaseModel):
        city: str

    async def weather(args: Weather, ctx) -> str:
        seen.append(args.city)
        return "21 celsius"

    tool = define_tool(name="get_weather", description="Get the current weather for a city.", schema=Weather, execute=weather)
    res = await Agent(AgentOptions(model=live_model(max_tokens=2000), max_steps=4, tools=[tool])).run(
        "Call the get_weather tool for Tokyo. Then state the temperature you got."
    )
    assert seen, "THE regression: without parameters on the wire the model cannot pass a city"
    assert seen[0].strip().lower().startswith("tokyo") or "tokyo" in seen[0].lower()
    assert any(c.name == "get_weather" for c in res.tool_calls)


async def test_live_structured_provider_strategy():
    agent = Agent(AgentOptions(
        model=live_model(max_tokens=2000),
        response_format=provider_strategy(Calc),
        max_steps=4,
    ))
    res = await agent.run('What is 6 times 7? Answer as {"answer": <number>}.')
    assert res.structured_response is not None
    assert res.structured_response.answer == 42


async def test_live_budget_fires_on_real_usage():
    agent = Agent(AgentOptions(model=live_model(max_tokens=400), budgets=TokenBudgets(max_tokens=1)))
    with pytest.raises(AgentError) as err:
        await agent.run("Reply with anything")
    assert err.value.code in ("BUDGET_EXCEEDED", "MODEL_ERROR")  # either guard may fire first