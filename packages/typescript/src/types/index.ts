import { z } from "zod";
import { ContentBlockSchema, type ContentBlock } from "./content.js";

export * from "./content.js";

export type Brand<T, B extends string> = T & { readonly __brand: B };

export type AgentId = Brand<string, "AgentId">;
export type RunId = Brand<string, "RunId">;
export type ToolName = Brand<string, "ToolName">;

export const ChatMessageSchema = z.object({
  id: z.string().optional(),
  role: z.enum(["system", "user", "assistant", "tool"]),
  content: z.union([z.string(), z.array(ContentBlockSchema)]),
  /** Provider user-identification; behavior varies, optional. */
  name: z.string().optional(),
  toolCallId: z.string().optional(),
  toolName: z.string().optional(),
  /** Pinned messages survive conversation-manager trimming. */
  pinned: z.boolean().optional(),
});

export interface ChatMessage {
  id?: string;
  role: "system" | "user" | "assistant" | "tool";
  content: string | ContentBlock[];
  name?: string;
  toolCallId?: string;
  toolName?: string;
  pinned?: boolean;
}

export interface AgentResult {
  text: string;
  messages: ChatMessage[];
  toolCalls: ExecutedToolCall[];
  usage: TokenUsage;
  /** Typed answer when the agent ran with `responseFormat`. */
  structuredResponse?: unknown;
  /** The invocation state object (same reference mutated during the run). */
  invocationState?: Record<string, unknown>;
}

export interface ExecutedToolCall {
  id: string;
  name: string;
  args: unknown;
  output: unknown;
  /** Downstream-only data (never sent to the model). See Tool.toResult. */
  artifact?: unknown;
}

export interface TokenUsage {
  inputTokens: number;
  outputTokens: number;
  /**
   * Reasoning/thinking tokens, when the provider reports them. A SUBSET of
   * `outputTokens` on every supported provider — count for reporting, never
   * add on top for budgets or cost.
   */
  reasoningTokens?: number;
  /** USD cost, when a price is known (see models/pricing). Never guessed. */
  costUsd?: number;
}

export type Result<T, E = AgentError> =
  | { ok: true; value: T }
  | { ok: false; error: E };

export class AgentError extends Error {
  readonly code: string;
  readonly retryable: boolean;
  constructor(code: string, message: string, retryable = false) {
    super(message);
    this.name = "AgentError";
    this.code = code;
    this.retryable = retryable;
  }
}

let messageSeq = 0;

/** Factory helpers — stable ids, no class hierarchy. */
export function system(content: string | ContentBlock[]): ChatMessage {
  return { id: `msg_${++messageSeq}`, role: "system", content };
}

export function user(content: string | ContentBlock[], opts?: { id?: string; name?: string }): ChatMessage {
  return { id: opts?.id ?? `msg_${++messageSeq}`, role: "user", content, name: opts?.name };
}

export function assistant(content: string | ContentBlock[]): ChatMessage {
  return { id: `msg_${++messageSeq}`, role: "assistant", content };
}

export function toolResult(toolCallId: string, toolName: string, content: string | ContentBlock[]): ChatMessage {
  return { id: `msg_${++messageSeq}`, role: "tool", content, toolCallId, toolName };
}

/** Serialize history for persistence (plain JSON-safe objects). */
export function serializeMessages(messages: ChatMessage[]): unknown[] {
  return messages.map((m) => ({ ...m }));
}

/** Deserialize + validate history (e.g. resume from disk). Throws on bad shape. */
export function deserializeMessages(raw: unknown[]): ChatMessage[] {
  return z.array(ChatMessageSchema).parse(raw);
}
