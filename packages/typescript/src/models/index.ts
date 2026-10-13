import type { ChatMessage, TokenUsage } from "../types/index.js";

export interface ToolCallRequest {
  /** Provider tool-call id; generated when the provider omits it. */
  id: string;
  name: string;
  args: unknown;
}

export interface ModelCompletion {
  text: string;
  /** Thinking/reasoning trace, when the provider exposes it. */
  thinking?: string;
  toolCalls: ToolCallRequest[];
  usage: TokenUsage;
  /**
   * Provider stop reason when reported ("stop", "length"/"max_tokens",
   * "tool_calls", ...). Consumers can detect truncation; a TOTAL truncation
   * (no content at all) throws MODEL_ERROR at the provider instead.
   */
  finishReason?: string;
}

export interface ResponseFormatWire {
  name: string;
  schema: Record<string, unknown>;
  strict?: boolean;
}

export interface CompleteOptions {
  messages: ChatMessage[];
  /** Tool metadata; `parametersSchema` (JSON Schema) lets models call with real args — omit only for zero-arg tools. */
  tools?: { name: string; description: string; parametersSchema?: Record<string, unknown> }[];
  /** Native structured output (OpenAI `response_format`). Honored when the provider supports it. */
  responseFormat?: ResponseFormatWire;
  /** Streamed text deltas (non-thinking). */
  onToken?: (delta: string) => void;
  /** Streamed thinking/reasoning deltas (reasoning_content). */
  onThinking?: (delta: string) => void;
  /** Aborts the in-flight provider request (fetch). */
  signal?: AbortSignal;
}

/** Provider abstraction — implement OpenAI/Anthropic/etc. behind this. */
export interface ModelProvider {
  readonly id: string;
  readonly capabilities?: { structuredOutput?: boolean; vision?: boolean; streaming?: boolean };
  complete(options: CompleteOptions): Promise<ModelCompletion>;
}

/** Deterministic mock for tests/examples — no network. */
export class MockModelProvider implements ModelProvider {
  readonly id = "mock";
  readonly capabilities = { structuredOutput: false, streaming: false };
  private calls = 0;

  async complete(_options: CompleteOptions): Promise<ModelCompletion> {
    this.calls += 1;
    return {
      text: `mock-response-${this.calls}`,
      toolCalls: [],
      usage: { inputTokens: 10, outputTokens: 5 },
    };
  }
}

export * from "./openai-compatible.js";
export * from "./capabilities.js";
export * from "./factory.js";
export * from "./anthropic.js";
export * from "./ollama.js";
export * from "./pricing.js";
