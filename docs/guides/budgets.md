# Budgets & pricing

Caps are how an agent run fails safely. There are two layers: `AgentOptions.budgets` (tokens/cost/deadline) and the limit middlewares ([middleware](middleware.md)).

## TokenBudgets

```ts
// TypeScript
budgets: {
  maxTokens?: number;    // total input + output tokens per run
  maxCostUsd?: number;   // USD cap per run; needs a known price
  prices?: ModelPrice;   // price override for unknown models
  deadlineMs?: number;   // wall-clock deadline per run
}
```

```python
# Python
TokenBudgets(max_tokens=None, max_cost_usd=None, prices=None, deadline_ms=None)
```

`name`: TS / Python respectively in this page.

## Enforcement model

- Checks run **after every model call**, against **cumulative** run usage — not once at the end.
- `maxTokens` compares `inputTokens + outputTokens`. `reasoningTokens` is a **subset of `outputTokens`** on every supported provider (OpenAI: `reasoning_tokens ⊂ completion_tokens`; Anthropic: thinking ⊂ output tokens), so it is never added on top — a double-count would fire budgets early.
- `deadlineMs` compares elapsed wall-clock since run start.
- Any breach throws `AgentError("BUDGET_EXCEEDED", ..., retryable=false)` — retry middleware passes these through untouched.
- Separately: `AgentOptions.maxToolCalls` caps executions (breach → `BUDGET_EXCEEDED`), and `modelCallLimit(maxCalls)` / `toolCallLimit(...)` middlewares enforce per-run model-call and per-tool budgets (see [middleware](middleware.md)).

## Pricing table + priceFor

Providers return token counts only, never cost — cost is always computed client-side as `usage × price` from a built-in table (`models/pricing.ts`, USD per 1M tokens):

| Model | input / 1M | output / 1M |
|---|---|---|
| `Qwen/Qwen3.5-9B` | 0.20 | 0.80 |
| `meta-llama/Llama-3.3-70B-Instruct-Turbo` | 0.88 | 0.88 |
| `deepseek-ai/DeepSeek-V4.1-Flash` | 0.20 | 0.60 |
| `zai-org/GLM-5.3-Flash` | 0.20 | 0.60 |
| `gpt-4o-mini` | 0.15 | 0.60 |
| `claude-sonnet-4-6` | 3.00 | 15.00 |
| `qwen2.5` | 0 | 0 |

These are researched defaults — **verify against your provider bill**; cloud prices change, and dedicated/self-hosted endpoints cost $0 in API terms.

`priceFor(modelId)` / `price_for(model_id)`:

- Provider prefixes (`openai:`, `anthropic:`, ...) are stripped before lookup.
- **Ollama collision rule**: an `ollama:`-prefixed id returns `{inputPerMillion: 0, outputPerMillion: 0}` *before* the table lookup — a local model whose name collides with a cloud model (e.g. `ollama:gpt-4o-mini`) runs free and must never be billed at cloud prices.
- Unknown models return `undefined`/`None` — never a misleading zero.

`ModelPrice` = `{ inputPerMillion, outputPerMillion }` (`ModelPrice(input_per_million, output_per_million)`).

## BUDGET_NO_PRICE

Enforcing `maxCostUsd` for a model that has no table entry and no `budgets.prices` override throws immediately:

```
AgentError("BUDGET_NO_PRICE",
  'Cannot enforce maxCostUsd: no price for model "<id>" (set budgets.prices)',
  retryable=false)
```

Costs are never guessed. Fix it by passing `prices: { inputPerMillion: 0.2, outputPerMillion: 0.8 }` (TS) or `ModelPrice(input_per_million=0.2, output_per_million=0.8)` (Python).

## usageCost math

```ts
cost = (usage.inputTokens * price.inputPerMillion
      + usage.outputTokens * price.outputPerMillion) / 1_000_000;
```

`usageCost(usage, modelId, override?)` / `usage_cost(usage, model_id, override=None)`:

- The override (`budgets.prices`) wins over the table, then the table, else `undefined`/`None`.
- Output tokens already include reasoning tokens, so they bill at the output rate with no extra addition.
- `AgentResult.usage.costUsd` (`usage.cost_usd`) is set **only when a price is known** — otherwise the field is absent/`None`, never a fake `$0`.

## Live-cost verification notes

- Verify the shipped prices against a real bill before relying on `maxCostUsd`; the table exists because TogetherAI/OpenAI-compatible and Anthropic usage payloads carry counts only (OpenRouter is the exception — it returns cost directly).
- Thinking models on chained prompts (e.g. the orchestration hand-off in `examples/live/02-orchestration.ts`) can burn the entire `maxTokens` on reasoning and trip `finish_reason=length`; the providers' truncation guards turn that into a loud non-retryable `MODEL_ERROR`, and `max_tokens`/`maxTokens` on the model must be sized so reasoning + content both fit. See [streaming & events](streaming.md) for the exact guards.

## Related

- [Streaming & events](streaming.md) — where usage numbers come from (`include_usage`)
- [Middleware & HITL](middleware.md) — call limits, retries that respect budget verdicts