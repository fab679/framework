"""LIVE: human-in-the-loop — suspend on a guarded tool, checkpoint, resume
with the TOOL-NAME WILDCARD decision (models re-issue args on resume).

NOTE: `except ... as first` DELETES the name at the end of the except block
(a Python gotcha) — stash it before leaving the block.
"""

import asyncio
import tempfile

from pydantic import BaseModel

from framework_agents import Agent, AgentOptions, FileCheckpointer, define_tool, init_chat_model, load_config
from framework_agents.middleware import HitlInterrupt, human_in_the_loop, model_retry

cfg = load_config()


class Publish(BaseModel):
    title: str
    word: str


async def main():
    cp = FileCheckpointer(tempfile.mkdtemp())
    model = lambda: init_chat_model(f"together:{cfg.together_model}", max_tokens=1200)

    async def publish(args: Publish, ctx) -> str:
        return f'published "{args.title}"'

    def mk():
        return Agent(AgentOptions(
            model=model(), max_steps=6, tools=[
                define_tool(
                    name="publish",
                    description="Publishes the report. ALWAYS call this tool before finishing.",
                    schema=Publish,
                    execute=publish,
                ),
            ],
            middleware=[model_retry(max_retries=2, initial_delay_ms=300), human_in_the_loop(interrupt_on={"publish": True})],
        ))

    suspended: dict = {}
    try:
        await mk().run('Write a one-sentence report about the word "falcon" and publish it.',
                       checkpointer=cp, thread_id="t1")
        print("NOTE: model finished without calling publish — model variance, rerun")
        return
    except HitlInterrupt as first:
        # Stash now: the `as` binding is deleted when the except block ends.
        suspended = {"tool": first.tool, "args": first.args}

    print("SUSPENDED:", suspended)
    # Human decision: WILDCARD bare tool name (models regenerate arg text; an
    # exact-key-only approval would never converge after resume).
    resumed = await mk().resume("t1", {suspended["tool"]: "approve"}, checkpointer=cp)
    published = next((c.output for c in resumed.tool_calls if c.name == "publish"), None)
    print("PUBLISHED:", str(published)[:60])
    print("RESUMED FINAL:", resumed.text.strip()[:60])
    print("PASS: suspend -> checkpoint -> wildcard-approve -> resume" if published else "NOTE: inspect")


asyncio.run(main())