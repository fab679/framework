import { OpenAICompatibleProvider } from "./openai-compatible.js";

export interface OllamaOptions {
  model: string;
  /** Defaults to local Ollama. No key needed. */
  baseUrl?: string;
  temperature?: number;
  maxTokens?: number;
  stream?: boolean;
  fetchFn?: typeof fetch;
}

/**
 * Local-first Ollama provider via its OpenAI-compatible endpoint.
 * No API key, no account, no network beyond localhost. Tool support
 * depends on the model (qwen2.5, llama3.1+ work well).
 * structuredOutput stays false → `auto` format uses the tool strategy.
 */
export class OllamaProvider extends OpenAICompatibleProvider {
  override readonly id: string;
  override readonly capabilities = { structuredOutput: false, streaming: true };

  constructor(opts: OllamaOptions) {
    super({
      baseUrl: opts.baseUrl ?? "http://localhost:11434/v1",
      apiKey: "ollama",
      model: opts.model,
      ...(opts.temperature != null ? { temperature: opts.temperature } : {}),
      ...(opts.maxTokens != null ? { maxTokens: opts.maxTokens } : {}),
      ...(opts.stream != null ? { stream: opts.stream } : {}),
      ...(opts.fetchFn ? { fetchFn: opts.fetchFn } : {}),
    });
    this.id = `ollama:${opts.model}`;
  }
}

export function ollama(opts: OllamaOptions): OllamaProvider {
  return new OllamaProvider(opts);
}
