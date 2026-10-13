import { mkdir, open, readFile, stat, unlink, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { AgentError } from "../types/index.js";
import type { ChatMessage } from "../types/index.js";

/**
 * Local-first durability: file checkpointer + file locks. No services,
 * no third-party deps. Checkpointed HITL resume works fully offline.
 */

export interface ThreadCheckpoint {
  threadId: string;
  savedAt: string;
  messages: ChatMessage[];
  state: Record<string, unknown>;
  pending: { interruptId: string; tool: string; args: unknown }[];
}

export interface Checkpointer {
  save(threadId: string, checkpoint: Omit<ThreadCheckpoint, "threadId" | "savedAt">): Promise<void>;
  load(threadId: string): Promise<ThreadCheckpoint | undefined>;
  delete(threadId: string): Promise<void>;
}

/**
 * Exclusive local lock via O_EXCL file creation. Stale locks (older than
 * staleMs) are reclaimed. Single-machine correctness; distributed locking
 * is explicitly out of scope (see roadmap P3).
 */
export async function withFileLock<T>(lockPath: string, fn: () => Promise<T>, opts: { staleMs?: number } = {}): Promise<T> {
  const staleMs = opts.staleMs ?? 30000;
  await mkdir(join(lockPath, ".."), { recursive: true });
  for (;;) {
    try {
      const handle = await open(lockPath, "wx");
      await handle.close();
      break;
    } catch (err) {
      const code = (err as NodeJS.ErrnoException).code;
      if (code !== "EEXIST") throw err;
      try {
        const info = await stat(lockPath);
        if (Date.now() - info.mtimeMs < staleMs) {
          throw new Error(`Lock held: ${lockPath}`);
        }
      } catch (statErr) {
        if ((statErr as NodeJS.ErrnoException).code !== "ENOENT") throw statErr;
      }
      try {
        await unlink(lockPath);
      } catch {
        // Raced with another releaser; retry acquisition.
      }
    }
  }
  try {
    return await fn();
  } finally {
    try {
      await unlink(lockPath);
    } catch {
      // Already released or reclaimed; ignore.
    }
  }
}

/** JSON-file checkpointer: <root>/threads/<threadId>/checkpoint.json, lock-guarded. */
export class FileCheckpointer implements Checkpointer {
  constructor(private root: string) {}

  /** threadId lands in a filesystem path — keep it a single safe directory name. */
  private threadDir(threadId: string): string {
    if (threadId === "" || threadId === "." || threadId === ".." || threadId.includes("/")) {
      throw new AgentError("THREAD_ID_INVALID", `Invalid threadId: ${JSON.stringify(threadId)}`, false);
    }
    return join(this.root, "threads", threadId);
  }

  private file(threadId: string): string {
    return join(this.threadDir(threadId), "checkpoint.json");
  }

  private lock(threadId: string): string {
    return join(this.threadDir(threadId), ".lock");
  }

  async save(threadId: string, checkpoint: Omit<ThreadCheckpoint, "threadId" | "savedAt">): Promise<void> {
    const record: ThreadCheckpoint = { threadId, savedAt: new Date().toISOString(), ...checkpoint };
    await withFileLock(this.lock(threadId), async () => {
      await mkdir(join(this.file(threadId), ".."), { recursive: true });
      await writeFile(this.file(threadId), JSON.stringify(record), "utf8");
    });
  }

  async load(threadId: string): Promise<ThreadCheckpoint | undefined> {
    return withFileLock(this.lock(threadId), async () => {
      try {
        return JSON.parse(await readFile(this.file(threadId), "utf8")) as ThreadCheckpoint;
      } catch {
        return undefined;
      }
    });
  }

  async delete(threadId: string): Promise<void> {
    await withFileLock(this.lock(threadId), async () => {
      try {
        await unlink(this.file(threadId));
      } catch {
        // Missing checkpoint is not an error.
      }
    });
  }
}
