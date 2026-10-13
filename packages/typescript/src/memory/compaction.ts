import { textOf, type ChatMessage } from "../types/index.js";
import type { Memory } from "../memory/index.js";

/**
 * Compaction: keep long runs inside the context window without losing meaning.
 * Strategy (P1): oldest messages -> model/pluggable summarizer -> single
 * boundary message. Semantic variant keeps extracted triples, drops prose.
 */

export interface CompactionOptions {
  /** Compact when messages exceed this count. Default 40. */
  maxMessages?: number;
  /** Summarizer: (messages to compress) -> summary text. Default: extractive stub. */
  summarize?: (messages: ChatMessage[]) => Promise<string>;
}

export function defaultSummarize(messages: ChatMessage[]): Promise<string> {
  const head = messages
    .filter((m) => m.role !== "system")
    .slice(0, 5)
    .map((m) => `[${m.role}] ${textOf(m.content).slice(0, 200)}`)
    .join("\n");
  return Promise.resolve(`Prior context summary (${messages.length} msgs):\n${head}`);
}

/** Wrap any Memory with auto-compaction. */
export class CompactedMemory implements Memory {
  constructor(
    private inner: Memory,
    private opts: CompactionOptions = {},
  ) {}

  async load(): Promise<ChatMessage[]> {
    return this.inner.load();
  }

  async save(messages: ChatMessage[]): Promise<void> {
    await this.inner.save(messages);
    const max = this.opts.maxMessages ?? 40;
    const current = await this.inner.load();
    if (current.length <= max) return;
    const summarize = this.opts.summarize ?? defaultSummarize;
    const overflow = current.slice(0, current.length - max + 1);
    const rest = current.slice(current.length - max + 1);
    const summary = await summarize(overflow);
    await this.inner.clear();
    await this.inner.save([{ role: "assistant", content: summary }, ...rest]);
  }

  async clear(): Promise<void> {
    return this.inner.clear();
  }
}
