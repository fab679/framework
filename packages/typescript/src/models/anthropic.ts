import { AgentError, hasImages, imagesOf, wireTextOf, type ChatMessage } from "../types/index.js";
import type { CompleteOptions, ModelCompletion, ModelProvider } from "./index.js";
import { assertCanHandle } from "./capabilities.js";

export interface AnthropicOptions {
  apiKey: string;
  model: string;
  baseUrl?: string;
  maxTokens?: number;
  temperature?: number;
  /** Extended thinking budget (tokens). Enables thinking blocks. */
  thinkingBudget?: number;
  stream?: boolean;
  fetchFn?: typeof fetch;
}

interface AnthropicBlock {
  type: string;
  text?: string;
  thinking?: string;
  id?: string;
  name?: string;
  input?: unknown;
}

/** Anthropic calls its truncation stop reason differently — normalize. */
function normalizeStopReason(stopReason?: string | null): string | undefined {
  if (!stopReason) return undefined;
  return stopReason === "max_tokens" ? "length" : stopReason;
}

/** Silent-empty guard: max_tokens burned with zero usable content. */
function assertNotEmptyTruncation(text: string, toolCalls: unknown[], finishReason: string | undefined, tokenCount: number): void {
  if (!text && !toolCalls.length && finishReason === "length") {
    throw new AgentError(
      "MODEL_ERROR",
      `Model stopped at max_tokens (${tokenCount} tokens) without producing content. Raise maxTokens.`,
      false,
    );
  }
}

/** Reasoning-split guard: finish_reason=stop with EMPTY text, no tool calls,
 *  and NON-EMPTY thinking means the whole answer landed in the reasoning
 *  channel (thinking models do this on short replies). Partial text with
 *  thinking keeps going. */
function assertNotEmptyReasoningSplit(text: string, toolCalls: unknown[], thinking: string, stopReason: string | undefined): void {
  if (!text && !toolCalls.length && thinking && stopReason === "stop") {
    throw new AgentError(
      "MODEL_ERROR",
      "Model stopped with stop_reason=stop having put the entire answer in the reasoning channel (empty content, no tool calls, thinking present). The reasoning/content split lost the reply — retry or raise maxTokens.",
      true,
    );
  }
}

function toAnthropicContent(m: ChatMessage): unknown {
  if (typeof m.content === "string") return m.content;
  const parts: unknown[] = [];
  const text = wireTextOf(m.content);
  if (text) parts.push({ type: "text", text });
  for (const img of imagesOf(m.content)) {
    parts.push({
      type: "image",
      source: img.data
        ? { type: "base64", media_type: img.mimeType ?? "image/jpeg", data: img.data }
        : { type: "url", url: img.url },
    });
  }
  for (const b of m.content) {
    if (b.type === "tool_call") parts.push({ type: "tool_use", id: b.id, name: b.name, input: b.args ?? {} });
  }
  return parts;
}

/**
 * Native Anthropic Messages API (fetch-based, no SDK).
 * - tools become `tools` with input_schema; tool results become tool_result blocks
 * - thinking blocks stream/return via onThinking
 * - no native json_schema mode: capabilities.structuredOutput is false,
 *   so `auto` responseFormat correctly falls back to the tool strategy
 */
export class AnthropicProvider implements ModelProvider {
  readonly id: string;
  readonly capabilities = { structuredOutput: false, streaming: true };
  private opts: AnthropicOptions;

  constructor(opts: AnthropicOptions) {
    if (!opts.apiKey) throw new Error("AnthropicProvider: apiKey is required (.env ANTHROPIC_API_KEY)");
    this.opts = { baseUrl: "https://api.anthropic.com", maxTokens: 1024, ...opts };
    // Extended thinking requires max_tokens > budget_tokens — fail at
    // construction with the fix, not as a confusing API 400 on first call.
    if (this.opts.thinkingBudget && this.opts.maxTokens != null && this.opts.maxTokens <= this.opts.thinkingBudget) {
      throw new Error(
        `AnthropicProvider: maxTokens (${this.opts.maxTokens}) must exceed thinkingBudget (${this.opts.thinkingBudget}); raise maxTokens`,
      );
    }
    this.id = `anthropic:${opts.model}`;
  }

  async complete(options: CompleteOptions): Promise<ModelCompletion> {
    const { fetchFn = fetch, apiKey, model, baseUrl, maxTokens, temperature, thinkingBudget, stream } = this.opts;
    const needsVision = options.messages.some((m) => hasImages(m.content));
    assertCanHandle(model, needsVision);

    const system = options.messages.filter((m) => m.role === "system").map((m) => wireTextOf(m.content)).join("\n\n");
    const messages: { role: "user" | "assistant"; content: unknown }[] = [];
    for (const m of options.messages.filter((x) => x.role !== "system")) {
      if (m.role === "tool") {
        // Anthropic expects ALL tool_results from one assistant turn in a
        // single user message — group consecutive tool messages.
        const result = { type: "tool_result", tool_use_id: m.toolCallId, content: toAnthropicContent(m) };
        const last = messages[messages.length - 1];
        if (last && last.role === "user" && Array.isArray(last.content) && last.content.some((b) => (b as { type?: string }).type === "tool_result")) {
          last.content.push(result);
          continue;
        }
        messages.push({ role: "user", content: [result] });
        continue;
      }
      messages.push({ role: m.role === "assistant" ? ("assistant" as const) : ("user" as const), content: toAnthropicContent(m) });
    }

    const body: Record<string, unknown> = {
      model,
      max_tokens: maxTokens,
      ...(system ? { system } : {}),
      ...(temperature != null ? { temperature } : {}),
      ...(thinkingBudget ? { thinking: { type: "enabled", budget_tokens: thinkingBudget } } : {}),
      ...(stream ? { stream: true } : {}),
      messages,
      ...(options.tools.length
        ? {
            tools: options.tools.map((t) => ({
              name: t.name,
              description: t.description,
              input_schema: t.parametersSchema ?? { type: "object" },
            })),
          }
        : {}),
    };

    let res: Response;
    try {
      res = await fetchFn(`${baseUrl!.replace(/\/$/, "")}/v1/messages`, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-api-key": apiKey,
          "anthropic-version": "2023-06-01",
          ...(stream ? { accept: "text/event-stream" } : {}),
        },
        body: JSON.stringify(body),
        ...(options.signal ? { signal: options.signal } : {}),
      });
    } catch (err) {
      throw new AgentError("MODEL_ERROR", `Model request failed: ${err instanceof Error ? err.message : String(err)}`, true);
    }
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      const retryable = res.status === 429 || res.status >= 500;
      throw new AgentError("MODEL_ERROR", `Model HTTP ${res.status}: ${text.slice(0, 300)}`, retryable);
    }
    return stream ? this.readStream(res, options) : this.readJson(res, options);
  }

  private toCompletion(blocks: AnthropicBlock[], usage: { inputTokens: number; outputTokens: number }, options: CompleteOptions): ModelCompletion {
    let text = "";
    let thinking = "";
    const toolCalls: ModelCompletion["toolCalls"] = [];
    for (const b of blocks) {
      if (b.type === "text" && b.text) {
        text += b.text;
        options.onToken?.(b.text);
      } else if (b.type === "thinking" && b.thinking) {
        thinking += b.thinking;
        options.onThinking?.(b.thinking);
      } else if (b.type === "tool_use" && b.name) {
        toolCalls.push({ id: b.id ?? `call_${toolCalls.length}`, name: b.name, args: b.input ?? {} });
      }
    }
    return { text, ...(thinking ? { thinking } : {}), toolCalls, usage };
  }

  private async readJson(res: Response, options: CompleteOptions): Promise<ModelCompletion> {
    const json = (await res.json()) as {
      content?: AnthropicBlock[];
      stop_reason?: string | null;
      usage?: { input_tokens: number; output_tokens: number };
    };
    const out = this.toCompletion(json.content ?? [], {
      inputTokens: json.usage?.input_tokens ?? 0,
      outputTokens: json.usage?.output_tokens ?? 0,
    }, options);
    const finishReason = normalizeStopReason(json.stop_reason);
    assertNotEmptyTruncation(out.text, out.toolCalls, finishReason, json.usage?.output_tokens ?? 0);
    assertNotEmptyReasoningSplit(out.text, out.toolCalls, out.thinking ?? "", finishReason);
    return finishReason ? { ...out, finishReason } : out;
  }

  private async readStream(res: Response, options: CompleteOptions): Promise<ModelCompletion> {
    if (!res.body) throw new Error("Streaming response has no body");
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let event = "";
    let inputTokens = 0;
    let outputTokens = 0;
    let stopReason: string | undefined;
    const blocks: AnthropicBlock[] = [];
    let current: AnthropicBlock | undefined;
    let jsonInput = "";

    const flush = (chunk: string) => {
      buffer += chunk;
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) {
        const t = line.trim();
        if (!t) continue;
        if (t.startsWith("event:")) {
          event = t.slice(6).trim();
          continue;
        }
        if (!t.startsWith("data:")) continue;
        const payload = t.slice(5).trim();
        if (!payload) continue;
        let data: Record<string, unknown>;
        try {
          data = JSON.parse(payload) as Record<string, unknown>;
        } catch {
          continue;
        }
        if (event === "message_start") {
          // message_start carries the input-side usage up front.
          const mu = ((data.message ?? {}) as { usage?: { input_tokens?: number } }).usage;
          if (mu?.input_tokens != null) inputTokens = mu.input_tokens;
        } else if (event === "message_delta") {
          // message_delta.usage.output_tokens is cumulative — assign, don't add.
          const du = (data.usage ?? {}) as { output_tokens?: number };
          if (du.output_tokens != null) outputTokens = du.output_tokens;
          const sr = (data.delta ?? {}) as { stop_reason?: string };
          if (sr.stop_reason) stopReason = normalizeStopReason(sr.stop_reason);
        } else if (event === "content_block_start") {
          const block = (data.content_block ?? {}) as AnthropicBlock;
          current = { type: block.type, id: block.id, name: block.name };
          jsonInput = "";
        } else if (event === "content_block_delta") {
          const delta = (data.delta ?? {}) as { type?: string; text?: string; thinking?: string; partial_json?: string };
          if (delta.text) {
            current = current ?? { type: "text" };
            current.text = (current.text ?? "") + delta.text;
          } else if (delta.thinking) {
            current = current ?? { type: "thinking" };
            current.thinking = (current.thinking ?? "") + delta.thinking;
          } else if (delta.partial_json) {
            jsonInput += delta.partial_json;
          }
        } else if (event === "content_block_stop") {
          if (current) {
            if (current.type === "tool_use") {
              try {
                current.input = JSON.parse(jsonInput || "{}") as unknown;
              } catch {
                current.input = {};
              }
            }
            blocks.push(current);
            current = undefined;
          }
        }
      }
    };

    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      flush(decoder.decode(value, { stream: true }));
    }
    flush(decoder.decode());
    if (current) blocks.push(current);
    const out = this.toCompletion(blocks, { inputTokens, outputTokens }, options);
    assertNotEmptyTruncation(out.text, out.toolCalls, stopReason, outputTokens);
    assertNotEmptyReasoningSplit(out.text, out.toolCalls, out.thinking ?? "", stopReason);
    return stopReason ? { ...out, finishReason: stopReason } : out;
  }
}
