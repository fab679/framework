"""LIVE: provider factory + cost — together (real), openai/anthropic (key-gated).

Add OPENAI_API_KEY / ANTHROPIC_API_KEY to framework/.env to light up the
other two sections. Anthropic usage IS live-tested in tests/ — this example
demonstrates the factory for every prefix."""
import asyncio
import os

from framework_agents import Agent, AgentOptions, init_chat_model, load_config

async def once(spec: str, **kw) -> None:
    try:
        model = init_chat_model(spec, max_tokens=kw.pop("max_tokens", 512), **kw)
    except ValueError as err:
        print(f"{spec}: gated — {err}")
        return
    try:
        res = await Agent(AgentOptions(model=model)).run("Reply with exactly: ok")
        cost = f"${res.usage.cost_usd:.6f}" if res.usage.cost_usd is not None else "no table price"
        print(f"{spec}: {res.text.strip()[:20]!r} | usage in={res.usage.input_tokens} out={res.usage.output_tokens} | {cost}")
    except Exception as err:  # noqa: BLE001 - demo shows real errors, never crashes
        print(f"{spec}: call failed -> {err}")

async def main():
    cfg = load_config()
    await once(f"together:{cfg.together_model}", max_tokens=1200)
    await once("openai:gpt-4o-mini")
    await once("anthropic:claude-sonnet-4-6", max_tokens=128)
    await once("ollama:qwen2.5")
    print("PASS: factory dispatch (gated sections skip with a clear reason)")

asyncio.run(main())
