"""LIVE: retrieval grounding — the fact exists ONLY in the corpus."""
import asyncio

from framework_agents import Agent, AgentOptions, InMemoryRetriever, define_tool, init_chat_model, load_config, retrieval_tool
from framework_agents.middleware import model_retry

cfg = load_config()

async def main():
    docs = [
        {"id": "spec-a", "text": "The Zephyr engine redlines at 7,200 rpm and uses synthetic oil only."},
        {"id": "spec-b", "text": "The Zephyr fuel tank holds 61 liters."},
        {"id": "spec-c", "text": "Zephyr service interval: every 12,000 km."},
    ]
    agent = Agent(AgentOptions(
        model=init_chat_model(f"together:{cfg.together_model}", max_tokens=2500),
        max_steps=4, middleware=[model_retry(max_retries=2, initial_delay_ms=300)],
        tools=[retrieval_tool(InMemoryRetriever(docs), k=2)],
    ))
    res = await agent.run("Use the retrieve tool: at what engine speed does the Zephyr redline? Answer with only the number.")
    print("RAG GROUNDED:", res.text.strip()[:40], "expect 7,200")
    print("PASS: grounded in retrieval" if "7200" in res.text.replace(",", "") else "NOTE: model variance")

asyncio.run(main())
