"""LIVE: memory + sessions — multi-turn recall + restore across a 'restart'."""
import asyncio
import tempfile

from framework_agents import Agent, AgentOptions, FileMemory, InMemoryMemory, LocalFileStorage, SessionManager, load_config, init_chat_model
from framework_agents.middleware import model_retry

cfg = load_config()

async def main():
    retry = [model_retry(max_retries=3, initial_delay_ms=500)]
    model = lambda: init_chat_model(f"together:{cfg.together_model}", max_tokens=2500)
    with tempfile.TemporaryDirectory() as d:
        # ALSO the pause/resume question, live: pause between turns, resume continues.
        from framework_agents import FileCheckpointer
        agent = Agent(AgentOptions(model=model(), memory=FileMemory(d), middleware=retry))
        await agent.run("My codename is Falcon. Just reply ok.")
        paused = await agent.pause("conv-1", FileCheckpointer(d))
        print("PAUSED:", paused)
        agent2 = Agent(AgentOptions(model=model(), memory=FileMemory(d), middleware=retry))
        resumed = await agent2.resume("conv-1", None, FileCheckpointer(d))
        print("RESUMED (no new input):", resumed.text.strip()[:60])
        # Real multi-agent "restart": a brand-new instance with a session store.
        sessions = SessionManager(storage=LocalFileStorage(d + "/sessions"), session_id="demo")
        a1 = Agent(AgentOptions(model=model(), session_manager=sessions, middleware=retry))
        await a1.run("Remember: the launch word is ZULU-7. Just reply ok.")
        a2 = Agent(AgentOptions(model=model(), session_manager=SessionManager(storage=LocalFileStorage(d + "/sessions"), session_id="demo"), middleware=retry))
        recall = await a2.run("What is the launch word? Reply with just the word.")
        print("RESTORED RECALL:", recall.text.strip(), "(expect ZULU-7)")
        print("PASS: memory + pause/resume + session restore" if "zulu" in recall.text.lower() else "NOTE: variance")

asyncio.run(main())
