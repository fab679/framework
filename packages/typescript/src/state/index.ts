/**
 * State in three lifetimes:
 * - Conversation history: messages (model-visible), via Memory.
 * - Agent state: validated JSON key-value store, NEVER sent to the model.
 * - Invocation state: per-run data + request handle, shared by reference
 *   across hooks and tools within one run, returned on the result.
 */

export type InvocationState = Record<string, unknown>;

function assertJsonSerializable(key: string, value: unknown): void {
  try {
    JSON.stringify(value);
  } catch {
    throw new Error(`AgentState: value for "${key}" is not JSON-serializable`);
  }
  if (typeof value === "function" || typeof value === "symbol" || typeof value === "bigint") {
    throw new Error(`AgentState: value for "${key}" is not JSON-serializable`);
  }
  // JSON.stringify silently turns these into {}/strings — reject at set()
  // instead of corrupting the value in every later snapshot/restore.
  if (value instanceof Map || value instanceof Set || value instanceof WeakMap || value instanceof WeakSet) {
    throw new Error(`AgentState: value for "${key}" is a ${value.constructor.name}; use a plain object/array`);
  }
  if (typeof value === "object" && value !== null && !(Array.isArray(value) || Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null)) {
    throw new Error(`AgentState: value for "${key}" has a custom prototype; plain objects round-trip, class instances don't`);
  }
}

/** Key-value store outside the model context. Tools + app logic read/write freely. */
export class AgentState {
  private data: Record<string, unknown>;

  constructor(initial: Record<string, unknown> = {}) {
    for (const [k, v] of Object.entries(initial)) assertJsonSerializable(k, v);
    this.data = { ...initial };
  }

  get<T = unknown>(key?: string): T | Record<string, unknown> | undefined {
    if (key === undefined) return { ...this.data };
    return this.data[key] as T | undefined;
  }

  set(key: string, value: unknown): void {
    assertJsonSerializable(key, value);
    this.data[key] = value;
  }

  delete(key: string): void {
    delete this.data[key];
  }

  /** Plain-object copy for snapshots/persistence. */
  toJSON(): Record<string, unknown> {
    return JSON.parse(JSON.stringify(this.data)) as Record<string, unknown>;
  }

  static fromJSON(raw: Record<string, unknown>): AgentState {
    return new AgentState(raw);
  }
}

/** What tools receive: agent identity + state + per-run invocation state. */
export interface ToolContext {
  agentName: string;
  state: AgentState;
  invocationState: InvocationState;
}
