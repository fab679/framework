# Structured output

Typed agent results instead of parsed prose: define a schema once and receive a validated object on `result.structuredResponse` (`structured_response` in Python) — or a thrown `STRUCTURED_*` error.

## Strategies

`responseFormat.strategy` (TypeScript) / `ResponseFormat.strategy` (Python) is `"auto"` (default) | `"provider"` | `"tool"`:

- **provider** — native `response_format` JSON Schema on OpenAI-compatible APIs. Most reliable. On parse failure, the loop appends the model's text plus a corrective user message (`Reply with corrected JSON only.`) and retries within `maxSteps`; exhausts into `STRUCTURED_INVALID` (retryable).
- **tool** — a synthetic tool named after the format (default `structured_response`) is appended. Validation runs inside the tool: valid output is returned (and captured as the structured result); invalid output comes back as a tool message the model repairs, up to `maxAttempts` (default `2`), then `STRUCTURED_NO_MATCH` (non-retryable).
- **auto** — resolves by provider capability at run time: `model.capabilities.structuredOutput` → provider, else tool. An unresolved `auto` threw on every run once; it is now pinned to resolve.

If a format is set and no valid result is produced within `maxSteps`, the run ends with `AgentError("STRUCTURED_NO_MATCH")`.

## TypeScript

```ts
import { Agent, initChatModel, providerStrategy, toolStrategy } from "framework-agents";
import { z } from "zod";

const Contact = z.object({ name: z.string(), email: z.string() });

const provider = new Agent({
  model: initChatModel("together:..."),
  responseFormat: providerStrategy(Contact),            // native response_format
});
// or:
const repaired = new Agent({
  model: initChatModel("together:..."),
  maxSteps: 3,
  responseFormat: toolStrategy(Contact),                // self-repairing tool
});

const res = await repaired.run("extract Ada");
res.structuredResponse; // { name: "Ada", email: "..." }
```

From the unit tests (`tests/unit/structured.test.ts`), pinned behavior:

- tool strategy captures validated output in `structuredResponse` even when the first attempt is invalid — the error feeds back as a tool message and the model self-repairs (asserted via an `"Error..."` tool message in `res.messages`).
- `maxSteps: 1` + a non-compliant model → `STRUCTURED_NO_MATCH`.
- provider strategy sends `response_format: { type: "json_schema", json_schema: { name: "structured_response", ... } }` on the wire (`json_schema.name` comes from the format name) and retries invalid JSON within `maxSteps`.
- `auto` picks provider for capable models (OpenAI-compatible `capabilities.structuredOutput === true`), tool otherwise (`MockModelProvider` has none).

## Python

```python
from pydantic import BaseModel

from framework_agents import Agent, AgentOptions, provider_strategy

class CalcAnswer(BaseModel):
    answer: int

agent = Agent(AgentOptions(
    model=...,
    response_format=provider_strategy(CalcAnswer),   # or tool_strategy(CalcAnswer)
    max_steps=3,
))
res = await agent.run("6*7?")
res.structured_response   # CalcAnswer(answer=42)
```

- `provider_strategy(schema, name="structured_response")` and `tool_strategy(schema, name=...)` mirror the TS factories; a bare `ResponseFormat(schema=MyModel)` defaults to `strategy="auto"`.
- The wire schema is `schema.model_json_schema()` (`ResponseFormatWire`).
- Provider parse failures call `model_validate_json` and re-prompt with the pydantic error inside `max_steps`; the tool strategy returns repair feedback (`"Error: invalid \"structured_response\": .... Fix the arguments and call again."`), then `STRUCTURED_NO_MATCH` after `max_attempts`.
- From the parity suite (`tests/test_core_parity.py`): a model advertising `capabilities = {"structured_output": True}` with `provider_strategy` yields `res.structured_response.answer == 42` — the pinned case for "auto resolves by capability".

## Result shape

The final run result carries the validated answer in both languages:

```ts
const res = await agent.run(input);
if (hasStructured) res.structuredResponse;   // present only when produced
```

The field is **absent/None** when no format was requested or nothing valid was produced — the run has already thrown `STRUCTURED_NO_MATCH` in that case, so an absent field only means "no format configured".

## Choosing a strategy

- OpenAI-compatible endpoint that implements `json_schema` response formats → `providerStrategy` (or leave `auto`).
- Endpoints without native structured support, or flows where you want the repair loop visible in the transcript → `toolStrategy`.
- Custom name: `providerStrategy(Contact, { name: "contact_card" })` / `provider_strategy(Contact, name="contact_card")` — the wire JSON-Schema name and the synthetic tool name both follow it.