import { mkdir, readdir, readFile, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { AgentError } from "../types/index.js";
import type { ChatMessage } from "../types/index.js";

/**
 * Sessions: persist agent state + history across restarts.
 * - Storage: byte-moving backends (memory / local files).
 * - SessionManager: latest snapshot + immutable checkpoints (UUID ids).
 * - Agent snapshots: manual take/load with field selection + appData.
 */

export interface Storage {
  read(key: string): Promise<string | undefined>;
  write(key: string, value: string): Promise<void>;
  deletePrefix(prefix: string): Promise<void>;
  list(prefix: string): Promise<string[]>;
}

export class InMemoryStorage implements Storage {
  private data = new Map<string, string>();
  async read(key: string): Promise<string | undefined> {
    return this.data.get(key);
  }
  async write(key: string, value: string): Promise<void> {
    this.data.set(key, value);
  }
  async deletePrefix(prefix: string): Promise<void> {
    for (const k of [...this.data.keys()]) if (k.startsWith(prefix)) this.data.delete(k);
  }
  async list(prefix: string): Promise<string[]> {
    return [...this.data.keys()].filter((k) => k.startsWith(prefix)).sort();
  }
}

export class LocalFileStorage implements Storage {
  constructor(private root: string) {}

  private path(key: string): string {
    // Trailing/duplicate slashes are harmless (directory markers); only
    // traversal segments can escape the root.
    const segments = key.split("/").filter((seg) => seg !== "");
    if (segments.includes("..") || key.includes("\0")) {
      throw new AgentError("STORAGE_KEY_INVALID", `Invalid storage key: ${JSON.stringify(key)}`, false);
    }
    return join(this.root, ...segments);
  }

  async read(key: string): Promise<string | undefined> {
    try {
      return await readFile(this.path(key), "utf8");
    } catch {
      return undefined;
    }
  }

  async write(key: string, value: string): Promise<void> {
    const full = this.path(key);
    await mkdir(join(full, ".."), { recursive: true });
    await writeFile(full, value, "utf8");
  }

  async deletePrefix(prefix: string): Promise<void> {
    await rm(this.path(prefix), { recursive: true, force: true });
  }

  async list(prefix: string): Promise<string[]> {
    const out: string[] = [];
    const walk = async (dir: string, rel: string): Promise<void> => {
      let entries;
      try {
        entries = await readdir(dir, { withFileTypes: true });
      } catch {
        return;
      }
      for (const e of entries) {
        if (e.isDirectory()) await walk(join(dir, e.name), `${rel}${e.name}/`);
        else out.push(`${prefix}${rel}${e.name}`);
      }
    };
    await walk(this.path(prefix), "");
    return out.sort();
  }
}

export interface SnapshotData {
  messages?: ChatMessage[];
  state?: Record<string, unknown>;
  systemPrompt?: string;
}

export interface Snapshot {
  id: string;
  createdAt: string;
  data: SnapshotData;
  appData?: Record<string, unknown>;
}

export type SnapshotField = "messages" | "state" | "systemPrompt";

export interface TakeSnapshotOptions {
  /** Preset bundle. Currently only "session" (messages + state). */
  preset?: "session";
  include?: SnapshotField[];
  exclude?: SnapshotField[];
  appData?: Record<string, unknown>;
}

const SESSION_PRESET: SnapshotField[] = ["messages", "state"];

export function selectSnapshotFields(
  full: Required<Pick<SnapshotData, "messages" | "state">> & Pick<SnapshotData, "systemPrompt">,
  opts: TakeSnapshotOptions = {},
): SnapshotData {
  const base = new Set<SnapshotField>(opts.preset === "session" ? SESSION_PRESET : []);
  for (const f of opts.include ?? []) base.add(f);
  for (const f of opts.exclude ?? []) base.delete(f);
  const data: SnapshotData = {};
  if (base.has("messages")) data.messages = full.messages;
  if (base.has("state")) data.state = full.state;
  if (base.has("systemPrompt")) data.systemPrompt = full.systemPrompt;
  return data;
}

function uuid(): string {
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
  });
}

export interface SessionManagerOptions {
  sessionId?: string;
  storage: Storage;
  agentId?: string;
}

export class SessionManager {
  readonly sessionId: string;
  readonly agentId: string;
  private storage: Storage;

  constructor(opts: SessionManagerOptions) {
    this.sessionId = opts.sessionId ?? uuid();
    this.agentId = opts.agentId ?? "default";
    this.storage = opts.storage;
  }

  private scope(): string {
    return `session/${this.sessionId}/scopes/agent/${this.agentId}/snapshots`;
  }

  async saveLatest(data: SnapshotData): Promise<void> {
    await this.storage.write(`${this.scope()}/snapshot_latest.json`, JSON.stringify({ data }));
  }

  async loadLatest(): Promise<SnapshotData | undefined> {
    const raw = await this.storage.read(`${this.scope()}/snapshot_latest.json`);
    if (!raw) return undefined;
    try {
      return (JSON.parse(raw) as { data: SnapshotData }).data;
    } catch {
      return undefined;
    }
  }

  /** Immutable checkpoint; returns its id. */
  async saveSnapshot(data: SnapshotData, appData?: Record<string, unknown>): Promise<string> {
    const snapshot: Snapshot = { id: uuid(), createdAt: new Date().toISOString(), data, appData };
    await this.storage.write(`${this.scope()}/immutable_history/snapshot_${snapshot.id}.json`, JSON.stringify(snapshot));
    return snapshot.id;
  }

  async listSnapshotIds(): Promise<string[]> {
    const keys = await this.storage.list(`${this.scope()}/immutable_history/`);
    return keys
      .map((k) => k.match(/snapshot_(.+)\.json$/)?.[1])
      .filter((id): id is string => !!id)
      .sort();
  }

  async restoreSnapshot(id: string): Promise<Snapshot | undefined> {
    const raw = await this.storage.read(`${this.scope()}/immutable_history/snapshot_${id}.json`);
    if (!raw) return undefined;
    try {
      return JSON.parse(raw) as Snapshot;
    } catch {
      return undefined;
    }
  }

  async deleteSession(): Promise<void> {
    await this.storage.deletePrefix(`session/${this.sessionId}/`);
  }
}
