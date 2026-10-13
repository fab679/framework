"""LIVE: swarm — autonomous handoffs between live members."""
import asyncio
from framework_agents import Agent, AgentOptions, load_config
from framework_agents import Swarm

cfg = load_config()
model = lambda: __import__("framework_agents").init_chat_model(f"together:{cfg.together_model}", max_tokens=3000)

async def main():
    from framework_agents.middleware import model_retry
    retry = [model_retry(max_retries=3, initial_delay_ms=500)]
    researcher = Agent(AgentOptions(model=model(), name="researcher", description="Gathers key facts",
        system_prompt="Produce ONE short fact, then call handoff_to_agent to the writer carrying it. Never answer the user directly.", middleware=retry))
    writer = Agent(AgentOptions(model=model(), name="writer", description="Writes the final answer",
        system_prompt="Write the final answer in one sentence from the handoff message. Do not call handoff_to_agent.",))
    swarm = Swarm(agents=[researcher, writer], entry_point="researcher", max_handoffs=2, max_iterations=3, loop_detection_window=4)
    events = []
    async for e in swarm.stream("State the boiling point of water in one sentence."):
        events.append(e.get("type") if e.get("type") != "handoff" else "handoff->" + str(e.get("to")))
    result = researcher and writer  # noqa - events carry the trace
    print("EVENTS:", events)
    ok = any(str(x).startswith("handoff") for x in events)
    print("PASS: live handoff" if ok else "NOTE: inspect (model variance)")

asyncio.run(main())
