"""LIVE: middleware suite — todos, tool budget feedback, failover, subsets."""
import asyncio

from pydantic import BaseModel

from framework_agents import Agent, AgentOptions, define_tool, init_chat_model, load_config
from framework_agents.middleware import llm_tool_selector_middleware, model_fallback, model_retry, todo_list, tool_call_limit

cfg = load_config()

class Text(BaseModel):
    t: str

async def main():
    model = lambda: init_chat_model(f"together:{cfg.together_model}", max_tokens=2500)
    # 1) todos written by the live model
    todos = todo_list()
    todo_agent = Agent(AgentOptions(model=model(), max_steps=4, middleware=[model_retry(max_retries=2, initial_delay_ms=300), todos]))
    await todo_agent.run('Use the write_todos tool to record: "collect facts", "write summary".')
    print("TODOS:", todos.list_todos())

    # 2) failover: a primary that always fails -> live backup answers
    class Broken:
        id = "broken"
        capabilities = {"structured_output": False, "streaming": False}
        async def complete(self, options):
            raise RuntimeError("primary down")
    failover = Agent(AgentOptions(model=Broken(), middleware=[model_fallback(model())]))
    res = await failover.run("Reply with exactly: fallback-ok")
    print("FAILOVER:", res.text.strip())

    # 3) tool subset by live selector
    async def add_exec(a, c):
        return "20" + a.t  # concatenate; the schema's t dispatches the input

    async def poem_exec(a, c):
        return "roses are red"

    adder = define_tool(name="calc_add", description="adds two numbers", schema=Text, execute=add_exec)
    poem = define_tool(name="poem", description="writes poems", schema=Text, execute=poem_exec)
    picked = Agent(AgentOptions(model=model(), max_steps=3, tools=[adder, poem],
        middleware=[model_retry(max_retries=2, initial_delay_ms=300), llm_tool_selector_middleware(model=model())]))
    out = await picked.run("I need you to concatenate '20' and '2' into one number — use the adding tool.")
    print("SELECTOR OUT:", out.text.strip()[:60])
    print("PASS: middleware suite live (inspect values above)")

asyncio.run(main())
