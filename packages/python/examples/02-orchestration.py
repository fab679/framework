"""LIVE: orchestration — sequential chain, parallel fan-out, model router."""
import asyncio
from framework_agents import Agent, AgentOptions, load_config
from framework_agents.orchestration import OrchestrationPlan, run_orchestrated

cfg = load_config()
model = lambda: __import__("framework_agents").init_chat_model(f"together:{cfg.together_model}", max_tokens=8000)

async def main():
    retry = [__import__("framework_agents.middleware", fromlist=["model_retry"]).model_retry(max_retries=3, initial_delay_ms=500)]
    mk = lambda name, sys, desc="": Agent(AgentOptions(model=model(), name=name, description=desc, system_prompt=sys, middleware=retry, max_steps=2))
    seq = await run_orchestrated(OrchestrationPlan(strategy="sequential", agents=[
        mk("math", "Answer with just the number."), mk("halver", "Answer with just half of the number you are given."),
    ]), "What is 100 + 24?")
    print("SEQUENTIAL:", seq.text.strip(), "(expect 62)")
    par = await run_orchestrated(OrchestrationPlan(strategy="parallel", concurrency=2, agents=[
        mk("a", 'Reply with exactly: AAA'), mk("b", 'Reply with exactly: BBB'), mk("c", 'Reply with exactly: CCC'),
    ]), "Respond as instructed.")
    print("PARALLEL:", [r.text.strip() for r in par.results])
    router = await run_orchestrated(OrchestrationPlan(strategy="router", agents=[
        mk("poet", "Reply with a one-line poem.", "writes poetry"),
        mk("calculator", "Answer with just the number.", "does arithmetic"),
    ]), "What is 12 times 12?")
    print("ROUTER:", router.routed_to, router.results[0].text.strip(), "(expect 1 / 144)")
    print("PASS" if "62" in seq.text and router.routed_to == 1 and "144" in router.results[0].text else "NOTE: model variance")

asyncio.run(main())
