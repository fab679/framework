import type { TokenUsage } from "../types/index.js";

/**
 * Per-model prices (USD per 1M tokens). Researched defaults — verify against
 * your provider bill: cloud prices change, and dedicated/self-hosted
 * endpoints (Ollama, vLLM) cost $0 in API terms.
 *
 * Why a table: TogetherAI/OpenAI-compatible and Anthropic usage payloads
 * carry token counts ONLY (no cost field). Cost is always computed
 * client-side as usage × price. (OpenRouter is the exception — it returns
 * cost directly; nothing to do there.)
 */

export interface ModelPrice {
  inputPerMillion: number;
  outputPerMillion: number;
}

const PRICES: Record<string, ModelPrice> = {
  "Qwen/Qwen3.5-9B": { inputPerMillion: 0.2, outputPerMillion: 0.8 },
  "meta-llama/Llama-3.3-70B-Instruct-Turbo": { inputPerMillion: 0.88, outputPerMillion: 0.88 },
  "deepseek-ai/DeepSeek-V4.1-Flash": { inputPerMillion: 0.2, outputPerMillion: 0.6 },
  "zai-org/GLM-5.3-Flash": { inputPerMillion: 0.2, outputPerMillion: 0.6 },
  "gpt-4o-mini": { inputPerMillion: 0.15, outputPerMillion: 0.6 },
  "claude-sonnet-4-6": { inputPerMillion: 3, outputPerMillion: 15 },
  "qwen2.5": { inputPerMillion: 0, outputPerMillion: 0 },
};

/** Look up a price by bare model id (provider prefixes like `openai:`/`ollama:` are stripped). */
export function priceFor(modelId: string): ModelPrice | undefined {
  // Local Ollama first: even a name colliding with a cloud model (e.g.
  // `ollama:gpt-4o-mini`) runs free, so it must win over the priced table.
  if (/^ollama:/.test(modelId)) return { inputPerMillion: 0, outputPerMillion: 0 };
  const bare = modelId.includes(":") ? modelId.split(":").slice(1).join(":") : modelId;
  if (bare in PRICES) return PRICES[bare];
  return undefined;
}

/**
 * USD cost for usage; unknown models return undefined (never a misleading zero).
 * `outputTokens` already includes reasoning tokens on every supported provider
 * (OpenAI: reasoning_tokens ⊂ completion_tokens; Anthropic: thinking ⊂ output_tokens),
 * so they are billed at the output rate with no extra addition.
 */
export function usageCost(usage: TokenUsage, modelId: string, override?: ModelPrice): number | undefined {
  const price = override ?? priceFor(modelId);
  if (!price) return undefined;
  return (usage.inputTokens * price.inputPerMillion + usage.outputTokens * price.outputPerMillion) / 1_000_000;
}
