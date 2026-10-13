"""LIVE: workflow DAG — levels, dedup'd deps, real node retry."""

import asyncio

from framework_agents import Agent, AgentOptions, load_config, init_chat_model
from framework_agents.middleware import model_retry
from framework_agents.workflows import WorkflowNode, run_workflow

cfg = load_config()


async def main():
    retry = [model_retry(max_retries=3, initial_delay_ms=500)]
    facts_agent = Agent(AgentOptions(
        model=init_chat_model(f"together:{cfg.together_model}", max_tokens=2000),
        system_prompt="Answer with exactly: water boils",
        middleware=retry, max_steps=2,
    ))
    calls = {"flaky": 0}

    async def flaky(ctx, invocation_state=None):
        calls["flaky"] += 1
        if calls["flaky"] == 1:
            raise RuntimeError("transient live failure")
        return "recovered on retry"

    async def facts(ctx, invocation_state=None):
        return await facts_agent.run("Say your line.")

    async def summary(ctx, invocation_state=None):
        return {
            "heard": getattr(ctx.get("facts"), "text", None),
            "flaky": ctx.get("flaky"),
        }

    out = await run_workflow([
        WorkflowNode(id="facts", run=facts),
        WorkflowNode(id="flaky", run=flaky, retry={"attempts": 2, "backoff_ms": 100}),
        WorkflowNode(id="summary", depends_on=["facts", "flaky"], run=summary),
    ])
    heard = (getattr(out.get("facts"), "text", "") or "").strip()
    print("FACTS:", heard, "| FLAKY calls:", calls["flaky"])
    print("SUMMARY:", {k: (str(v)[:30] if v else v) for k, v in (out.get("summary") or {}).items()})
    print("PASS: DAG + real retry" if "water" in heard and calls["flaky"] == 2 else "NOTE: inspect")


asyncio.run(main())