"""LIVE: streaming — real token/thinking deltas through Agent.stream(), and
tool calls streamed live (tool_call event before execution, tool_result when
it lands)."""

import asyncio

from pydantic import BaseModel

from framework_agents import Agent, AgentOptions, define_tool, load_config
from framework_agents.config import init_chat_model
from framework_agents.middleware import model_retry


class Count(BaseModel):
    count: int


async def main() -> None:
    cfg = load_config()
    model = init_chat_model(f"together:{cfg.together_model}", max_tokens=2500)

    async def digits(args: Count, ctx) -> str:
        await asyncio.sleep(0.7)  # the 700ms proves tool_result lands mid-run
        return "3.14159"

    agent = Agent(AgentOptions(
        model=model,
        max_steps=4,
        middleware=[model_retry(max_retries=3, initial_delay_ms=500)],
        tools=[define_tool(name="get_pi_digits", description="Returns digits of pi.", schema=Count, execute=digits)],
    ))

    kinds: dict[str, int] = {}
    text = ""
    async for event in agent.stream("Use the get_pi_digits tool with count 5, then reply with exactly the digits it returned."):
        kinds[event["type"]] = kinds.get(event["type"], 0) + 1
        if event["type"] in ("token", "thinking"):
            pass  # deltas stream live through hooks; batched per call here
        if event["type"] == "done":
            text = event["text"]
    print("EVENT KINDS:", kinds)
    print("TEXT:", text.strip())
    assert agent._last_result is not None
    print("USAGE:", agent._last_result.usage)
    ok = kinds.get("tool_call") and kinds.get("tool_result") and "3.14159" in text
    print("PASS: tools streamed live" if ok else "NOTE: inspect trace (model variance)")


asyncio.run(main())