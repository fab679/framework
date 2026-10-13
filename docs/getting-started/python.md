# Getting started: Python

The Python package is a parity port of the TypeScript reference implementation. Every pinned semantic (budgets, clear-then-save memory, structured auto-strategy, HITL resume) matches the fixed TS behavior; see [cross-language notes](../guides/agents-and-tools.md) per topic.

## Install

From PyPI:

```bash
pip install framework-agents
```

Or from the repository (development install with test extras):

```bash
cd packages/python
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Requirements: Python >= 3.11. Runtime dependencies are `pydantic` and `httpx` only. Import as `framework_agents`.

## Environment

`load_config()` reads the same keys as the TS `loadConfig()`, with the same defaults and the same walk-up `.env` loading (cwd, then three parents; `FRAMEWORK_ENV_PATH` overrides). TogetherAI wins when `TOGETHER_API_KEY` is set.

| Key | Purpose |
|---|---|
| `TOGETHER_API_KEY` / `TOGETHER_BASE_URL` / `TOGETHER_MODEL` | TogetherAI (OpenAI-compatible, default chat) |
| `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` | OpenAI fallback |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_BASE_URL` / `ANTHROPIC_MODEL` | Anthropic native API |
| `OLLAMA_BASE_URL` / `OLLAMA_MODEL` | local Ollama (no key) |

`init_chat_model(spec)` resolves `provider:model` strings: `together:` (default for bare ids), `openai:`, `anthropic:`, `ollama:`, `custom:` (needs `base_url` + `api_key`).

## First agent + first tool

`define_tool` takes `name`, `description`, a pydantic `schema` (the validated argument shape — its JSON Schema reaches the provider wire; without it tools are advertised zero-arg), and `execute`:

```python
import asyncio
from pydantic import BaseModel

from framework_agents import Agent, AgentOptions, define_tool, init_chat_model


class Weather(BaseModel):
    city: str


async def get_weather(args: Weather, ctx) -> dict:
    return {"city": args.city, "tempC": 21}


agent = Agent(AgentOptions(
    name="assistant",
    model=init_chat_model("together:Qwen/Qwen3.5-9B", max_tokens=2500, stream=True),
    tools=[define_tool(
        name="get_weather",
        description="Current weather for a city",
        schema=Weather,
        execute=get_weather,
    )],
    system_prompt="You are a concise weather assistant.",
    max_steps=6,  # default 6
))

result = asyncio.run(agent.run("Weather in Tokyo?"))
print(result.text, result.tool_calls, result.usage)
```

The second `execute` parameter is an optional `ToolContext` (`agent_name`, `state`, `invocation_state`) — see [agents & tools](../guides/agents-and-tools.md).

## Streaming

`agent.stream()` returns an async iterator of the same typed events as TS (`step | token | thinking | tool_call | tool_result | done`). Python generators cannot return a final value, so the finished `AgentResult` is reachable on the stream wrapper via `.result`:

```python
stream = agent.stream("Weather in Tokyo?")
async for event in stream:
    if event["type"] == "token":
        print(event["delta"], end="")
    elif event["type"] == "tool_call":
        print("calling", event["name"])
    elif event["type"] == "done":
        print("\nfinal:", event["text"])
result = stream.result  # captured per stream at the done event
```

`collect_stream(stream)` buckets both events and result. Details in [streaming](../guides/streaming.md).

## Budgets

Caps are enforced after **every** model call on cumulative usage:

```python
from framework_agents import AgentOptions, TokenBudgets

agent = Agent(AgentOptions(
    model=init_chat_model("together:Qwen/Qwen3.5-9B"),
    budgets=TokenBudgets(
        max_tokens=8000,      # reasoning tokens are a subset of output, never added
        max_cost_usd=0.05,   # needs a known price or budgets.prices
        deadline_ms=60_000,
    ),
    max_tool_calls=10,        # separate option; exceeded → BUDGET_EXCEEDED
))
```

Exceeding any cap raises `AgentError("BUDGET_EXCEEDED", retryable=False)`. See [budgets & pricing](../guides/budgets.md).

## Run the offline test suite

The parity suite is fully offline (mock/scripted models):

```bash
cd packages/python
.venv/bin/python -m pytest tests -q
```

Live tests are gated behind a marker and hit the real Together API:

```bash
E2E_MODEL=1 .venv/bin/python -m pytest tests -q -m live
```

Live examples live in `examples/` — see [live examples](live-examples.md).

## Next steps

- [Agents & tools](../guides/agents-and-tools.md)
- [Memory & sessions](../guides/memory-sessions.md)
- [Middleware & HITL](../guides/middleware.md)
- [A2A & serving](../guides/a2a-serving.md)


## "Do I have to `await` everything?"

Only what touches the network or disk - which is the same rule the TypeScript
package follows (`await agent.run(...)` works there too; Python just needs a
one-line event loop at your entry point, the equivalent of JS top-level await).

| Always `await` / `async for` | Always plain |
| --- | --- |
| `run` / `stream` (async for) / `resume` / `pause` / `branch` / `edit_message` | constructors (`Agent`, options dataclasses, providers) |
| `Memory`: `load` / `save` / `clear` ; `Storage`: all ops ; `Checkpointer`: all ops | `define_tool` + every middleware factory |
| provider `complete` ; streamed tools inside the loop call | `init_chat_model` / `load_config` / `price_for` / `usage_cost` |
| orchestration / workflows / swarm / evals / conformance (`run_*`) | message factories + `serialize_messages` / `text_of` |
| A2A client `send` / `get_card` / `stream` ; server `start` / `stop` | `state.get` / `set` / `to_json` |
| semantic store ops, `review_ontology` / `propose_shapes` / `adopt_shapes` / `migrate_t_box` | `quads_to_json_ld` / `json_ld_to_quads` / `parse_skill_file` / `match_skills` |

Notes:

- **Tool `execute` is awaited by the loop** - you write `async def weather(...)`,
  you never await it yourself. Same for `AgentStream`: `async for` over it, the
  final result lands in `stream.result` (or `agent.run`'s return).
- There is deliberately **no sync-mode clone** of the API: a sync/async duality
  would maintain two inconsistent surfaces. If you want a blocking one-liner at
  the top level, it is just `asyncio.run(agent.run("hi"))`.
