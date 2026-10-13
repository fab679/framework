import { loadConfig } from "../config/index.js";
import type { ModelProvider } from "./index.js";
import { OpenAICompatibleProvider, type OpenAICompatibleOptions } from "./openai-compatible.js";
import { AnthropicProvider } from "./anthropic.js";
import { OllamaProvider } from "./ollama.js";

/**
 * Resolve `provider:model` strings to a configured provider.
 * - `together:<id>` / bare `<id>` → TogetherAI (TOGETHER_API_KEY)
 * - `openai:<id>` → OpenAI (OPENAI_API_KEY)
 * - `anthropic:<id>` → Anthropic native API (ANTHROPIC_API_KEY)
 * - `ollama:<id>` → local Ollama (no key; OLLAMA_BASE_URL or localhost:11434)
 * - `custom:<id>` + baseUrl/apiKey → any OpenAI-compatible endpoint
 */
export function initChatModel(
  spec: string,
  opts: {
    apiKey?: string;
    baseUrl?: string;
    temperature?: number;
    maxTokens?: number;
    stream?: boolean;
    thinkingBudget?: number;
  } = {},
): ModelProvider {
  const cfg = loadConfig();
  const extra: Partial<OpenAICompatibleOptions> = {
    ...(opts.temperature != null ? { temperature: opts.temperature } : {}),
    ...(opts.maxTokens != null ? { maxTokens: opts.maxTokens } : {}),
    ...(opts.stream != null ? { stream: opts.stream } : {}),
  };
  const colon = spec.indexOf(":");
  const provider = colon > 0 ? spec.slice(0, colon) : "together";
  const model = colon > 0 ? spec.slice(colon + 1) : spec;
  switch (provider) {
    case "together": {
      const apiKey = opts.apiKey ?? cfg.TOGETHER_API_KEY;
      if (!apiKey) throw new Error("initChatModel: set TOGETHER_API_KEY in .env (or pass apiKey)");
      return new OpenAICompatibleProvider({ baseUrl: opts.baseUrl ?? cfg.TOGETHER_BASE_URL, apiKey, model, ...extra });
    }
    case "openai": {
      const apiKey = opts.apiKey ?? cfg.OPENAI_API_KEY;
      if (!apiKey) throw new Error("initChatModel: set OPENAI_API_KEY in .env (or pass apiKey)");
      return new OpenAICompatibleProvider({ baseUrl: opts.baseUrl ?? cfg.OPENAI_BASE_URL, apiKey, model, ...extra });
    }
    case "anthropic": {
      const apiKey = opts.apiKey ?? cfg.ANTHROPIC_API_KEY;
      if (!apiKey) throw new Error("initChatModel: set ANTHROPIC_API_KEY in .env (or pass apiKey)");
      return new AnthropicProvider({
        apiKey,
        model,
        ...(opts.baseUrl ? { baseUrl: opts.baseUrl } : { baseUrl: cfg.ANTHROPIC_BASE_URL }),
        ...(opts.thinkingBudget != null ? { thinkingBudget: opts.thinkingBudget } : {}),
        ...extra,
      });
    }
    case "ollama": {
      return new OllamaProvider({ model, ...(opts.baseUrl ? { baseUrl: opts.baseUrl } : { baseUrl: cfg.OLLAMA_BASE_URL }), ...extra });
    }
    case "custom": {
      if (!opts.baseUrl || !opts.apiKey) throw new Error("initChatModel: custom: needs baseUrl + apiKey");
      return new OpenAICompatibleProvider({ baseUrl: opts.baseUrl, apiKey: opts.apiKey, model, ...extra });
    }
    default:
      throw new Error(`initChatModel: unknown provider "${provider}" (use together:|openai:|anthropic:|ollama:|custom:)`);
  }
}
