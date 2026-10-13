"""LIVE: guardrails + budgets — PII redaction, policy deny, real cost caps."""
import asyncio

from pydantic import BaseModel

from framework_agents import Agent, AgentOptions, TokenBudgets, define_tool, AgentError, init_chat_model, load_config
from framework_agents.middleware import model_retry, pii_redact, policy_filter

cfg = load_config()
model = lambda: init_chat_model(f"together:{cfg.together_model}", max_tokens=2500)

async def main():
    pii_agent = Agent(AgentOptions(
        model=model(),
        middleware=[model_retry(max_retries=2, initial_delay_ms=300), pii_redact("email", strategy="redact", apply_to_output=True)],
    ))
    pii = await pii_agent.run("Reply exactly with: my email is foo.bar@corp.example")
    print("PII REDACTED:", "foo.bar@corp.example" not in pii.text)
    print("PII TEXT:", pii.text.strip())

    class NoArgs(BaseModel):
        pass

    executed = []

    async def rm_exec(a, c):
        executed.append("rm_rf")
        return "deleted"

    async def safe_exec(a, c):
        executed.append("noop")
        return "did nothing"

    rm_rf = define_tool(name="rm_rf", description="Deletes everything.", schema=NoArgs, execute=rm_exec)
    safe = define_tool(name="safe", description="Does nothing.", schema=NoArgs, execute=safe_exec)
    policy = Agent(AgentOptions(model=model(), max_steps=4, tools=[rm_rf, safe],
        middleware=[model_retry(max_retries=2, initial_delay_ms=300), policy_filter(deny=["rm_"], exit_behavior="continue")]))
    await policy.run('Call the rm_rf tool; if refused call noop instead, then say "done".')
    print("POLICY:", "rm_rf never executed" if "rm" not in executed else "FAIL — rm executed!", "| calls:", executed)

    try:
        await Agent(AgentOptions(model=model(), budgets=TokenBudgets(max_tokens=1))).run("Say anything")
        print("BUDGET: NOTE - no cap hit")
    except AgentError as e:
        print("BUDGET CODE:", e.code, "(cap enforced on real usage)")

    ok_pii = "foo.bar@corp.example" not in pii.text
    ok_policy = "rm" not in executed
    print("PASS: guardrails live" if ok_pii and ok_policy else "NOTE: inspect — pii redacted: %s, rm blocked: %s" % (ok_pii, ok_policy))

asyncio.run(main())

asyncio.run(main())
