import type { ChatMessage, ContentBlock } from "../types/index.js";
import { AgentError, hasImages, imagesOf, wireTextOf } from "../types/index.js";
import type { CompleteOptions, ModelCompletion, ModelProvider } from "./index.js";
import { assertCanHandle } from "./capabilities.js";

export interface OpenAICompatibleOptions {
  baseUrl: string;
  apiKey: string;
  model: string;
  temperature?: number;
  maxTokens?: number;
  /** Stream responses (SSE) so text + thinking tokens flow via callbacks. Default false. */
  stream?: boolean;
  /** fetch-compatible; injectable for tests. Defaults to global fetch. */
  fetchFn?: typeof fetch;
}

interface ChoiceMessage {
  content?: string | null;
  /** Thinking trace: Together sends `reasoning`, vLLM/DeepSeek `reasoning_content`. */
  reasoning?: string | null;
  reasoning_content?: string | null;
  tool_calls?: { id?: string; function: { name: string; arguments: string } }[];
}

interface StreamDelta {
  content?: string | null;
  /** Thinking deltas: Together sends `reasoning`, vLLM/DeepSeek `reasoning_content`. */
  reasoning?: string | null;
  reasoning_content?: string | null;
  tool_calls?: { id?: string; index?: number; function: { name?: string; arguments?: string } }[];
}

/** Reasoning-token count: Together reports it top-level, OpenAI nested in details. */
function reasoningOf(usage: {
  reasoning_tokens?: number;
  completion_tokens_details?: { reasoning_tokens?: number };
}): number | undefined {
  return usage.reasoning_tokens ?? usage.completion_tokens_details?.reasoning_tokens;
}

/** Thinking trace across provider field spellings. */
function thinkingOf(m: { reasoning?: string | null; reasoning_content?: string | null }): string | undefined {
  const trace = m.reasoning ?? m.reasoning_content;
  return trace ? trace : undefined;
}

function toWireRole(role: ChatMessage["role"]): string {
  if (role === "tool") return "tool";
  return role;
}

function toWireContent(m: ChatMessage): unknown {
  if (typeof m.content === "string" && !hasImages(m.content)) return m.content;
  const text = wireTextOf(m.content);
  const imgs = imagesOf(m.content);
  if (!imgs.length) return text;
  return [
    ...(text ? [{ type: "text", text }] : []),
    ...imgs.map((img) => ({
      type: "image_url",
      image_url: { url: img.data ? `data:${img.mimeType ?? "image/jpeg"};base64,${img.data}` : img.url },
    })),
  ];
}

function toWireMessage(m: ChatMessage): Record<string, unknown> {
  const base: Record<string, unknown> = { role: toWireRole(m.role), content: toWireContent(m) };
  if (m.toolCallId) base.tool_call_id = m.toolCallId;
  if (typeof m.content !== "string") {
    const calls = m.content.filter(
      (b): b is Extract<ContentBlock, { type: "tool_call" }> => b.type === "tool_call",
    );
    if (calls.length) {
      base.tool_calls = calls.map((c) => ({
        id: c.id,
        type: "function",
        function: { name: c.name, arguments: JSON.stringify(c.args ?? {}) },
      }));
    }
  }
  return base;
}

/**
 * Minimal OpenAI-compatible chat provider (OpenAI, TogetherAI, vLLM, etc.).
 * POST {baseUrl}/chat/completions with tools; no SDK dependency.
 * - Images: fail fast with MODEL_CAPABILITY when the model can't see.
 * - Thinking: reasoning_content streams through onThinking (SSE when stream: true).
 */
export class OpenAICompatibleProvider implements ModelProvider {
  readonly id: string;
  readonly capabilities = { structuredOutput: true, streaming: true };
  private opts: OpenAICompatibleOptions;

  constructor(opts: OpenAICompatibleOptions) {
    if (!opts.apiKey) throw new Error("OpenAICompatibleProvider: apiKey is required (.env TOGETHER_API_KEY / OPENAI_API_KEY)");
    this.opts = { temperature: 0.2, ...opts };
    this.id = `openai-compatible:${opts.model}`;
  }

  async complete(options: CompleteOptions): Promise<ModelCompletion> {
    const { fetchFn = fetch, baseUrl, apiKey, model, temperature, maxTokens, stream } = this.opts;
    const needsVision = options.messages.some((m) => hasImages(m.content));
    assertCanHandle(model, needsVision);

    const url = `${baseUrl.replace(/\/$/, "")}/chat/completions`;
    const body = {
      model,
      temperature,
      ...(maxTokens ? { max_tokens: maxTokens } : {}),
      // stream_options makes OpenAI-compatible servers attach usage to the final
  // chunk; without it streaming runs report {0,0} — budgets/cost would be fiction.
  ...(stream ? { stream: true, stream_options: { include_usage: true } } : {}),
      ...(options.responseFormat
        ? {
            response_format: {
              type: "json_schema",
              json_schema: {
                name: options.responseFormat.name,
                strict: options.responseFormat.strict ?? true,
                schema: options.responseFormat.schema,
              },
            },
          }
        : {}),
      messages: options.messages.map(toWireMessage),
      ...(options.tools?.length
        ? {
            tools: (options.tools ?? []).map((t) => ({
              type: "function",
              function: {
                name: t.name,
                description: t.description,
                // Without parameters the API advertises a zero-arg tool.
                ...(t.parametersSchema ? { parameters: t.parametersSchema } : {}),
              },
            })),
          }
        : {}),
    };

    let res: Response;
    try {
      res = await fetchFn(url, {
        method: "POST",
        headers: { "content-type": "application/json", authorization: `Bearer ${apiKey}` },
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
    return stream
      ? this.readStream(res, options)
      : this.readJson(res, options);
  }

  private async readJson(res: Response, options: CompleteOptions): Promise<ModelCompletion> {
    const json = (await res.json()) as {
      choices: { message: ChoiceMessage; finish_reason?: string | null }[];
      usage?: { prompt_tokens: number; completion_tokens: number; reasoning_tokens?: number; completion_tokens_details?: { reasoning_tokens?: number } };
    };
    const choice = json.choices[0];
    const msg = choice?.message ?? {};
    const thinking = thinkingOf(msg);
    if (thinking) options.onThinking?.(thinking);
    if (msg.content) options.onToken?.(msg.content);
    const finishReason = choice?.finish_reason ?? undefined;
    // Reasoning-split guard: finish_reason=stop with EMPTY content, no tool
    // calls, and NON-EMPTY thinking means the whole answer landed in the
    // reasoning channel (thinking models do this on short replies). Partial
    // content with thinking keeps going.
    if (!msg.content && !(msg.tool_calls ?? []).length && thinking && finishReason === "stop") {
      throw new AgentError(
        "MODEL_ERROR",
        "Model stopped with finish_reason=stop having put the entire answer in the reasoning channel (empty content, no tool calls, thinking present). The reasoning/content split lost the reply — retry or raise maxTokens.",
        true,
      );
    }
    // Silent-empty guard: a thinking model can burn its WHOLE maxTokens
    // budget before emitting any content. That must never come back as a
    // successful empty answer callers can't distinguish from a real reply.
    if (!msg.content && !(msg.tool_calls ?? []).length && finishReason === "length") {
      throw new AgentError(
        "MODEL_ERROR",
        `Model spent its entire max_tokens budget (${json.usage?.completion_tokens ?? "?"} tokens, finish_reason=length) thinking without producing content. Raise maxTokens.`,
        false,
      );
    }
    const reasoningTokens = json.usage ? reasoningOf(json.usage) : undefined;
    return {
      text: msg.content ?? "",
      ...(thinking ? { thinking } : {}),
      toolCalls: parseToolCalls(msg.tool_calls ?? []),
      ...(finishReason ? { finishReason } : {}),
      usage: {
        inputTokens: json.usage?.prompt_tokens ?? 0,
        outputTokens: json.usage?.completion_tokens ?? 0,
        ...(reasoningTokens != null ? { reasoningTokens } : {}),
      },
    };
  }

  /** SSE: `data: {...}` lines; delta.content -> onToken, delta.reasoning_content -> onThinking. */
  private async readStream(res: Response, options: CompleteOptions): Promise<ModelCompletion> {
    if (!res.body) throw new Error("Streaming response has no body");
    return this._readSse(res.body, options).catch((err) => {
      // aborts rethrown; AgentErrors carry their (delivered-aware) verdict
      if (err instanceof AgentError || (err instanceof Error && err.name === "AbortError")) throw err;
      throw new AgentError("MODEL_ERROR", `Stream broke mid-response: ${err}`, false);
    });
  }

  private async _readSse(body: ReadableStream<Uint8Array>, options: CompleteOptions): Promise<ModelCompletion> {
    // Retry-verdict rule: once deltas reach the caller, a mid-stream failure
    // is NON-retryable (re-running would duplicate already-streamed text);
    // before any delta, failures stay retryable (nothing was delivered).
    let delivered = false;
    try {
      const reader = body.getReader();
      const decoder = new TextDecoder();
      let text = "";
      let thinking = "";
      let inputTokens = 0;
      let outputTokens = 0;
      let reasoningTokens: number | undefined;
      let finishReason: string | undefined;
      const toolArgBuffers = new Map<number, { id: string; name: string; args: string }>();
      let buffer = "";

      const flush = (chunk: string) => {
        buffer += chunk;
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";
        for (const line of lines) {
          const t = line.trim();
          if (!t.startsWith("data:")) continue;
          const payload = t.slice(5).trim();
          if (payload === "[DONE]") continue;
          let evt: {
            choices?: { delta?: StreamDelta; finish_reason?: string | null }[];
            usage?: null | { prompt_tokens?: number; completion_tokens?: number; reasoning_tokens?: number; completion_tokens_details?: { reasoning_tokens?: number } };
          };
          try {
            evt = JSON.parse(payload);
          } catch {
            continue;
          }
          if (evt.choices?.[0]?.finish_reason) finishReason = evt.choices[0].finish_reason ?? undefined;
          // The usage-only final chunk has an empty/partial choices array and a
          // NON-NULL usage Together sends `"usage": null` on content chunks.
          if (evt.usage) {
            if (evt.usage.prompt_tokens != null) inputTokens = evt.usage.prompt_tokens;
            if (evt.usage.completion_tokens != null) outputTokens = evt.usage.completion_tokens;
            const rt = reasoningOf(evt.usage);
            if (rt != null) reasoningTokens = rt;
            continue;
          }
          const delta = evt.choices?.[0]?.delta;
          if (!delta) continue;
          const think = thinkingOf(delta);
          if (think) {
            thinking += think;
            // Thinking deltas reach the caller like token deltas do — the
            // delivered verdict covers them (as well as tool fragments).
            delivered = true;
            options.onThinking?.(think);
          }
          if (delta.content) {
            text += delta.content;
            delivered = true;
            options.onToken?.(delta.content);
          }
          for (const [i, tc] of (delta.tool_calls ?? []).entries()) {
            const key = tc.index ?? i;
            // Tool-call fragments are delivered state too: a retry would
            // re-issue a call mid-assembly, so treat the attempt as spent.
            delivered = true;
            const slot = toolArgBuffers.get(key) ?? { id: "", name: "", args: "" };
            if (tc.id) slot.id = tc.id;
            if (tc.function?.name) slot.name = tc.function.name;
            if (tc.function?.arguments) slot.args += tc.function.arguments;
            toolArgBuffers.set(key, slot);
          }
        }
      };

      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        flush(decoder.decode(value, { stream: true }));
      }
      flush(decoder.decode());

      // Same reasoning-split guard as readJson: answer landed in the reasoning
      // channel and the stream carried NO content or tool calls.
      if (!text && !toolArgBuffers.size && thinking && finishReason === "stop") {
        throw new AgentError(
          "MODEL_ERROR",
          "Stream ended at finish_reason=stop having put the entire answer in the reasoning channel (no content, no tool calls, thinking present). The reasoning/content split lost the reply \u2014 retry or raise maxTokens.",
          true,
        );
      }
      // Same silent-empty guard as readJson: thought-past-budget stream.
      if (!text && !toolArgBuffers.size && finishReason === "length") {
        throw new AgentError(
          "MODEL_ERROR",
          `Stream ended at finish_reason=length with no content (${outputTokens} tokens burned). Raise maxTokens.`,
          false,
        );
      }
      return {
        text,
        ...(thinking ? { thinking } : {}),
        ...(finishReason ? { finishReason } : {}),
        toolCalls: [...toolArgBuffers.values()]
          // Streamed ids must not repeat across turns (Anthropic rejects duplicates).
          .map((t) => ({ id: t.id || `call_${++callSeq}`, name: t.name, args: safeJson(t.args) })),
        usage: {
          inputTokens,
          outputTokens,
          ...(reasoningTokens != null ? { reasoningTokens } : {}),
        },
      };
    } catch (err) {
      if (err instanceof Error && err.name === "AbortError") throw err;
      // Explicit AgentError verdicts (reasoning-split / silent-empty guards)
      // pass through untouched.
      if (err instanceof AgentError) throw err;
      // delivered-aware verdict: deltas already streamed => non-retryable
      throw new AgentError("MODEL_ERROR", `Stream broke mid-response${delivered ? " (deltas already delivered)" : ""}: ${err}`, !delivered);
    }
  }
}

let callSeq = 0;

function parseToolCalls(calls: { id?: string; function: { name: string; arguments: string } }[]): ModelCompletion["toolCalls"] {
  return calls.map((c) => ({ id: c.id ?? `call_${++callSeq}`, name: c.function.name, args: safeJson(c.function.arguments || "{}") }));
}

function safeJson(raw: string): unknown {
  try {
    return JSON.parse(raw || "{}");
  } catch {
    return {};
  }
}

/** Build a provider from env config (TogetherAI preferred when key present). */
export function providerFromEnv(
  active: { baseUrl: string; apiKey: string; model: string },
  extra?: Partial<OpenAICompatibleOptions>,
): OpenAICompatibleProvider {
  return new OpenAICompatibleProvider({ baseUrl: active.baseUrl, apiKey: active.apiKey, model: active.model, ...extra });
}
