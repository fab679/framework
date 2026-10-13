import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { AgentError, deserializeMessages, serializeMessages, type ChatMessage } from "../types/index.js";

export interface Memory {
  load(): Promise<ChatMessage[]>;
  save(messages: ChatMessage[]): Promise<void>;
  clear(): Promise<void>;
}

export class InMemoryMemory implements Memory {
  private messages: ChatMessage[] = [];
  private maxMessages: number;

  constructor(maxMessages = 50) {
    this.maxMessages = maxMessages;
  }

  async load(): Promise<ChatMessage[]> {
    return [...this.messages];
  }

  async save(messages: ChatMessage[]): Promise<void> {
    this.messages = [...this.messages, ...messages].slice(-this.maxMessages);
  }

  async clear(): Promise<void> {
    this.messages = [];
  }
}

/**
 * File-backed JSON memory: same windowing as in-memory, persisted per
 * write to `<dir>/memory.json`. Crash-safe enough for local-first use
 * (write-then-rename atomicity, validated reload).
 */
export class FileMemory implements Memory {
  private cache?: ChatMessage[];
  private file: string;
  private maxMessages: number;

  constructor(dir: string, maxMessages = 50) {
    this.file = join(dir, "memory.json");
    this.maxMessages = maxMessages;
  }

  private async readAll(): Promise<ChatMessage[]> {
    if (this.cache) return this.cache;
    let raw: string;
    try {
      raw = await readFile(this.file, "utf8");
    } catch {
      // Missing file = fresh memory (first run).
      this.cache = [];
      return this.cache;
    }
    try {
      this.cache = deserializeMessages(JSON.parse(raw) as unknown[]);
    } catch (err) {
      // Corrupted file must FAIL loudly: returning [] would make the next
      // save() overwrite whatever was still on disk — silent total data loss.
      throw new AgentError(
        "MEMORY_CORRUPT",
        `Memory file is not valid JSON for messages (${this.file}): ${err instanceof Error ? err.message : String(err)}`,
        false,
      );
    }
    return this.cache;
  }

  private async flush(): Promise<void> {
    await mkdir(dirname(this.file), { recursive: true });
    // Unique tmp per flush: concurrent saves sharing one fixed name race
    // on the rename and can publish a torn or half-written file.
    const tmp = `${this.file}.${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}.tmp`;
    await writeFile(tmp, JSON.stringify(serializeMessages(this.cache ?? [])), "utf8");
    await rename(tmp, this.file);
  }

  async load(): Promise<ChatMessage[]> {
    return [...(await this.readAll())];
  }

  async save(messages: ChatMessage[]): Promise<void> {
    const all = [...(await this.readAll()), ...messages].slice(-this.maxMessages);
    this.cache = all;
    await this.flush();
  }

  async clear(): Promise<void> {
    this.cache = [];
    await this.flush();
  }
}
