import { estimateTokens } from "../utils/index.js";
import { textOf, type ChatMessage } from "../types/index.js";
import type { ModelProvider } from "../models/index.js";

/**
 * Conversation managers keep history within context limits.
 * - apply(): after-run management (before memory.save).
 * - beforeModel(): per-turn management inside the loop (sliding window).
 * - reduce(): explicit shrink (overflow recovery / proactive triggers).
 * Pinned messages are never removed. Tool/assistant-tool_call pairs are
 * never orphaned: trimming drops leading dangling messages.
 */

export interface ConversationManager {
  readonly name: string;
  /** Messages removed so far (lets session loading skip efficiently). */
  removedCount: number;
  apply(messages: ChatMessage[]): ChatMessage[] | Promise<ChatMessage[]>;
  reduce(messages: ChatMessage[]): ChatMessage[] | Promise<ChatMessage[]>;
  beforeModel?(messages: ChatMessage[], step: number): ChatMessage[] | Promise<ChatMessage[]>;
}

function hasOnlyToolCalls(content: ChatMessage["content"]): boolean {
  if (typeof content === "string") return false;
  const blocks = content.filter((b) => b.type === "tool_call" || (b.type === "text" && b.text.trim() === ""));
  return blocks.length > 0 && blocks.length === content.length;
}

/** Drop leading messages that lost their pair (tool head, call-only assistant head). */
export function trimDangling(messages: ChatMessage[]): ChatMessage[] {
  let i = 0;
  while (i < messages.length) {
    const m = messages[i];
    if (m.pinned) break;
    if (m.role === "tool") {
      i += 1;
      continue;
    }
    if (m.role === "assistant" && hasOnlyToolCalls(m.content)) {
      i += 1;
      continue;
    }
    break;
  }
  return messages.slice(i);
}

export function messageTokens(m: ChatMessage): number {
  const text = typeof m.content === "string" ? m.content : textOf(m.content);
  return estimateTokens(text);
}

export function historyTokens(messages: ChatMessage[]): number {
  return messages.reduce((n, m) => n + messageTokens(m), 0);
}

/** Keeps full history. Pass explicitly to disable the default sliding window. */
export class NullConversationManager implements ConversationManager {
  readonly name = "null";
  removedCount = 0;
  apply(messages: ChatMessage[]): ChatMessage[] {
    return messages;
  }
  reduce(messages: ChatMessage[]): ChatMessage[] {
    return messages;
  }
}

export interface SlidingWindowOptions {
  /** Max messages kept (excluding pinned). Default 40. */
  windowSize?: number;
  /** Permanently protect the first N messages. Default 0. */
  pinFirst?: number;
  /** Manage before every model call (true) or every N calls. Default false (after-run only). */
  perTurn?: boolean | number;
}

export class SlidingWindowConversationManager implements ConversationManager {
  readonly name = "sliding-window";
  removedCount = 0;
  private windowSize: number;
  private pinFirst: number;
  private perTurn: boolean | number;

  constructor(opts: SlidingWindowOptions = {}) {
    this.windowSize = Math.max(1, opts.windowSize ?? 40);
    this.pinFirst = Math.max(0, opts.pinFirst ?? 0);
    this.perTurn = opts.perTurn ?? false;
  }

  private pin(messages: ChatMessage[]): void {
    for (let i = 0; i < Math.min(this.pinFirst, messages.length); i++) messages[i].pinned = true;
  }

  private trim(messages: ChatMessage[]): ChatMessage[] {
    this.pin(messages);
    const keep = new Set<ChatMessage>();
    for (const m of messages) if (m.pinned) keep.add(m);
    const rest = messages.filter((m) => !m.pinned);
    const dropped = Math.max(0, rest.length - this.windowSize);
    this.removedCount += dropped;
    for (const m of rest.slice(dropped)) keep.add(m);
    // Merge in ORIGINAL order: hoisting pinned messages to the front would
    // reorder the conversation whenever a mid-history message is pinned.
    return trimDangling(messages.filter((m) => keep.has(m)));
  }

  apply(messages: ChatMessage[]): ChatMessage[] {
    this.pin(messages);
    return this.trim(messages);
  }

  reduce(messages: ChatMessage[]): ChatMessage[] {
    return this.trim(messages);
  }

  beforeModel(messages: ChatMessage[], step: number): ChatMessage[] {
    if (this.perTurn === false) return messages;
    if (typeof this.perTurn === "number" && (step % this.perTurn !== 0 || this.perTurn <= 0)) return messages;
    return this.trim(messages);
  }
}

export interface SummarizingOptions {
  /** Fraction of oldest messages to summarize away (0.1–0.8). Default 0.3. */
  summaryRatio?: number;
  /** Newest messages always kept. Default 10. */
  preserveRecent?: number;
  pinFirst?: number;
  summarizer?: (messages: ChatMessage[]) => Promise<string>;
  /** Model used for summaries when no `summarizer` fn is given. */
  model?: ModelProvider;
  summaryPrefix?: string;
}

export class SummarizingConversationManager implements ConversationManager {
  readonly name = "summarizing";
  removedCount = 0;
  private ratio: number;
  private preserveRecent: number;
  private pinFirst: number;
  private summarizer?: (messages: ChatMessage[]) => Promise<string>;
  private model?: ModelProvider;
  private summaryPrefix: string;
  /** Set when the last apply() summarized (tests/UI can assert on it). */
  lastSummary?: string;

  constructor(opts: SummarizingOptions = {}) {
    this.ratio = Math.min(0.8, Math.max(0.1, opts.summaryRatio ?? 0.3));
    this.preserveRecent = Math.max(1, opts.preserveRecent ?? 10);
    this.pinFirst = Math.max(0, opts.pinFirst ?? 0);
    this.summarizer = opts.summarizer;
    this.model = opts.model;
    this.summaryPrefix = opts.summaryPrefix ?? "Summary of earlier conversation:";
  }

  private summarizable(messages: ChatMessage[]): { head: ChatMessage[]; tail: ChatMessage[] } {
    for (let i = 0; i < Math.min(this.pinFirst, messages.length); i++) messages[i].pinned = true;
    const eligible = messages.filter((m) => !m.pinned);
    const count = Math.min(eligible.length - this.preserveRecent, Math.floor(eligible.length * this.ratio));
    if (count <= 0) return { head: [], tail: messages };
    const cutoff = new Set(eligible.slice(0, count));
    return { head: messages.filter((m) => cutoff.has(m)), tail: messages.filter((m) => !cutoff.has(m)) };
  }

  private async summarize(head: ChatMessage[]): Promise<string> {
    if (this.summarizer) return this.summarizer(head);
    if (!this.model) {
      return head
        .map((m) => `[${m.role}] ${(typeof m.content === "string" ? m.content : textOf(m.content)).slice(0, 200)}`)
        .join("\n");
    }
    const transcript = head.map((m) => `[${m.role}] ${typeof m.content === "string" ? m.content : textOf(m.content)}`).join("\n");
    const res = await this.model.complete({
      messages: [{ role: "user", content: `Summarize concisely, preserving decisions, names, tool outcomes:\n${transcript}` }],
      tools: [],
    });
    return res.text;
  }

  async apply(messages: ChatMessage[]): Promise<ChatMessage[]> {
    const { head, tail } = this.summarizable(messages);
    if (!head.length) return messages;
    const summary = await this.summarize(head);
    this.lastSummary = summary;
    this.removedCount += head.length - 1;
    return [{ role: "assistant", content: `${this.summaryPrefix}\n${summary}` } as ChatMessage, ...trimDangling(tail)];
  }

  reduce(messages: ChatMessage[]): ChatMessage[] {
    return messages;
  }
}
