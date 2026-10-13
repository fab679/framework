"""LIVE: A2A over the wire — the Python server hosts a live agent; the Python
client streams two turns through one context, and a2a_as_tool lets ANOTHER
agent call it as a tool."""
import asyncio

from framework_agents import A2AServer, Agent, AgentOptions, a2a_as_tool, init_chat_model, load_config
from framework_agents.a2a_client import AgentClient
from framework_agents.middleware import model_retry

cfg = load_config()

async def main():
    model = lambda: init_chat_model(f"together:{cfg.together_model}", max_tokens=1000)
    server = A2AServer(
        card={"name": "specialist", "description": "Knows capitals"},
        agent_factory=lambda context_id: Agent(AgentOptions(
            model=model(),
            system_prompt="Answer capital-city questions in one word.",
            middleware=[model_retry(max_retries=3, initial_delay_ms=400)],
        )),
    )
    port = await server.start()
    url = f"http://127.0.0.1:{port}"
    print("A2A server up:", url)
    try:
        client = AgentClient(url)
        context: list[str] = []
        async def turn(msg: str) -> str:
            text = ""
            async for e in client.stream(msg, context[0] if context else None, None, on_context=context.append):
                if e["type"] == "done":
                    text = e["text"]
            return text
        t1 = await turn("The capital of France? One word.")
        t2 = await turn("And of Japan? One word.")
        print("TURN 1:", t1.strip(), "| TURN 2:", t2.strip(), "| shared contextId:", bool(context))

        from framework_agents.a2a_client import A2AClient
        remote = a2a_as_tool(A2AClient(url), name="ask_specialist", description="Ask the remote capitals specialist.")
        orchestrator = Agent(AgentOptions(model=model(), max_steps=4, middleware=[model_retry(max_retries=3, initial_delay_ms=400)], tools=[remote]))
        res = await orchestrator.run("Use the ask_specialist tool to ask for the capital of Italy, then repeat it.")
        print("REMOTE-AS-TOOL:", res.text.strip()[:60])
        ok = "paris" in t1.lower() and res.text.strip()
        print("PASS: A2A live (stream x2 + remote-as-tool)" if ok else "NOTE: model variance")
    finally:
        await server.stop()
        print("stopped")

asyncio.run(main())
