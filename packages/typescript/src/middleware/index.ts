import { AgentError } from "../types/index.js";
import type { InvocationState } from "../state/index.js";
import { sleep } from "../utils/index.js";
import type { ChatMessage, ContentBlock } from "../types/index.js";
import type { ModelCompletion, ModelProvider, ResponseFormatWire } from "../models/index.js";
import type { Tool } from "../tools/index.js";

/**
 * Middleware — control the agent loop without forking it.
 *
 * Execution order:
 * - beforeModel / beforeTool: registration order
 * - afterModel / afterTool: REVERSE order
 * - wrapModel / aroundTool: nested, first registered = outermost
 */

export interface HookContext {
  step: number;
  /** Per-run data, shared by reference (never sent to the model). */
  invocationState?: InvocationState;
}

export interface ModelRequest {
  messages: ChatMessage[];
  tools: { name: string; description: string; parametersSchema?: Record<string, unknown> }[];
  responseFormat?: ResponseFormatWire;
}

export interface ToolCallRef {
  name: string;
  args: unknown;
}

export interface AgentMiddleware {
  name: string;
  /** Tools contributed by this middleware (merged into the agent's tool list). */
  tools?: Tool[];
  beforeModel?: (req: ModelRequest, ctx: HookContext) => Promise<ModelRequest | undefined> | ModelRequest | undefined;
  wrapModel?: (
    req: ModelRequest,
    next: (req: ModelRequest) => Promise<ModelCompletion>,
    ctx: HookContext,
  ) => Promise<ModelCompletion>;
  afterModel?: (res: ModelCompletion, req: ModelRequest, ctx: HookContext) => Promise<ModelCompletion | undefined> | ModelCompletion | undefined;
  beforeTool?: (call: ToolCallRef, ctx: HookContext) => Promise<ToolCallRef | undefined> | ToolCallRef | undefined;
  aroundTool?: (call: ToolCallRef, next: () => Promise<unknown>, ctx: HookContext) => Promise<unknown>;
  afterTool?: (call: ToolCallRef, output: unknown, ctx: HookContext) => Promise<unknown> | unknown;
}

/** Identity factory — documents intent and locks the type. */
export function createMiddleware(m: AgentMiddleware): AgentMiddleware {
  return m;
}

/** Thrown (usually by approval middleware) to skip execution with feedback for the model. */
export class ToolRejection extends Error {
  readonly feedback: string;
  constructor(feedback: string) {
    super(feedback);
    this.name = "ToolRejection";
    this.feedback = feedback;
  }
}

// ---------------------------------------------------------------------------
// Retry verdicts (stampRetryable / getRetryable)
// ---------------------------------------------------------------------------

const RETRYABLE = Symbol.for("framework.retryable");

/** Mark an error retryable (true) or fatal (false). Shape and type are untouched. */
export function stampRetryable<T extends Error>(err: T, retryable: boolean): T {
  (err as unknown as Record<symbol, boolean>)[RETRYABLE] = retryable;
  return err;
}

/** Read a verdict: true/false, or undefined when never marked. AgentError maps to its flag. */
export function getRetryable(err: unknown): boolean | undefined {
  if (err instanceof AgentError) return err.retryable;
  if (err instanceof Error) return (err as unknown as Record<symbol, boolean | undefined>)[RETRYABLE];
  return undefined;
}

export interface BackoffOptions {
  maxRetries?: number;
  initialDelayMs?: number;
  backoffFactor?: number;
  maxDelayMs?: number;
  jitter?: boolean;
}

export function backoffDelayMs(attempt: number, opts: Required<Omit<BackoffOptions, "maxRetries">>): number {
  const grown = opts.initialDelayMs * opts.backoffFactor ** attempt;
  const capped = Math.min(grown, opts.maxDelayMs);
  if (!opts.jitter) return capped;
  return capped * (0.75 + Math.random() * 0.5);
}

function normalizeBackoff(opts: BackoffOptions): Required<Omit<BackoffOptions, "maxRetries">> & { maxRetries: number } {
  return {
    maxRetries: Math.max(0, opts.maxRetries ?? 2),
    initialDelayMs: Math.max(0, opts.initialDelayMs ?? 1000),
    backoffFactor: Math.max(0, opts.backoffFactor ?? 2),
    maxDelayMs: Math.max(0, opts.maxDelayMs ?? 60000),
    jitter: opts.jitter ?? true,
  };
}

export type RetryFilter = ((error: Error) => boolean) | (new (...args: never[]) => Error)[];

function shouldRetryErr(filter: RetryFilter | undefined, err: unknown): boolean {
  if (!(err instanceof Error)) return true;
  if (getRetryable(err) === false) return false;
  if (!filter) return getRetryable(err) ?? true;
  if (Array.isArray(filter)) return filter.some((C) => err instanceof C);
  return filter(err);
}

// ---------------------------------------------------------------------------
// Built-ins
// ---------------------------------------------------------------------------

/** Cap model calls per run; exceeded -> AgentError(BUDGET_EXCEEDED). Stateless (uses step). */
export function modelCallLimit(maxCalls: number): AgentMiddleware {
  return {
    name: "model-call-limit",
    beforeModel: (_req, ctx) => {
      if (ctx.step >= maxCalls) {
        throw new AgentError("BUDGET_EXCEEDED", `Exceeded model call limit (${maxCalls})`, false);
      }
      return undefined;
    },
  };
}

/** Retry failing tool executions with exponential backoff. AgentErrors pass through untouched. */
export function toolRetry(opts: { attempts?: number; backoffMs?: number } = {}): AgentMiddleware {
  const attempts = Math.max(1, opts.attempts ?? 2);
  const backoffMs = opts.backoffMs ?? 0;
  return {
    name: "tool-retry",
    aroundTool: async (call, next, _ctx) => {
      let last: unknown;
      for (let i = 0; i < attempts; i++) {
        try {
          return await next();
        } catch (err) {
          if (err instanceof AgentError) throw err;
          last = err;
          if (i < attempts - 1 && backoffMs > 0) await sleep(backoffMs);
        }
      }
      throw last;
    },
  };
}

export interface RetryMiddlewareOptions extends BackoffOptions {
  retryOn?: RetryFilter;
  onFailure?: "error" | "continue" | ((error: Error) => string);
  /** Limit retries to these tool names (tool retry only). */
  tools?: string[];
}

/**
 * Full tool retry: exponential backoff + jitter, error-class filtering,
 * per-tool scoping, and exhausted-failure behavior (continue as tool
 * message vs rethrow). Honors stampRetryable verdicts.
 */
export function toolRetryMiddleware(opts: RetryMiddlewareOptions = {}): AgentMiddleware {
  const cfg = normalizeBackoff(opts);
  const only = opts.tools ? new Set(opts.tools) : undefined;
  return {
    name: "tool-retry",
    aroundTool: async (call, next) => {
      if (only && !only.has(call.name)) return next();
      let last: unknown;
      for (let i = 0; i <= cfg.maxRetries; i++) {
        try {
          return await next();
        } catch (err) {
          last = err;
          const retryable = err instanceof Error ? shouldRetryErr(opts.retryOn, err) : true;
          if (!retryable || i === cfg.maxRetries) break;
          await sleep(backoffDelayMs(i, cfg));
        }
      }
      if (opts.onFailure === "error") throw last;
      if (typeof opts.onFailure === "function") return opts.onFailure(last as Error);
      if (opts.onFailure === "continue") {
        // Degrade gracefully: the error becomes tool feedback, the run finishes.
        const message = last instanceof Error ? last.message : String(last);
        return `Tool "${call.name}" failed after retries: ${message}`;
      }
      throw last;
    },
  };
}

/**
 * Model retry with the same backoff/filter/onFailure semantics.
 * onFailure "continue" synthesizes an empty-text completion so the loop ends gracefully.
 */
export function modelRetryMiddleware(opts: RetryMiddlewareOptions = {}): AgentMiddleware {
  const cfg = normalizeBackoff(opts);
  return {
    name: "model-retry",
    wrapModel: async (req, next) => {
      let last: unknown;
      for (let i = 0; i <= cfg.maxRetries; i++) {
        try {
          return await next(req);
        } catch (err) {
          last = err;
          const retryable = err instanceof Error ? shouldRetryErr(opts.retryOn, err) : true;
          if (!retryable || i === cfg.maxRetries) break;
          await sleep(backoffDelayMs(i, cfg));
        }
      }
      if (opts.onFailure === "error") throw last;
      if (typeof opts.onFailure === "function") {
        return { text: opts.onFailure(last as Error), toolCalls: [], usage: { inputTokens: 0, outputTokens: 0 } };
      }
      if (opts.onFailure === "continue") {
        const message = last instanceof Error ? last.message : String(last);
        return { text: `Model call failed after retries: ${message}`, toolCalls: [], usage: { inputTokens: 0, outputTokens: 0 } };
      }
      throw last;
    },
  };
}

/** Fall back across providers when the primary model fails (first success wins). */
export function modelFallbackMiddleware(...fallbacks: ModelProvider[]): AgentMiddleware {
  return {
    name: "model-fallback",
    wrapModel: async (req, next) => {
      try {
        return await next(req);
      } catch (primaryErr) {
        let last = primaryErr;
        for (const fb of fallbacks) {
          try {
            return await fb.complete({
              messages: req.messages,
              tools: req.tools,
              ...(req.responseFormat ? { responseFormat: req.responseFormat } : {}),
            });
          } catch (err) {
            last = err;
          }
        }
        throw last;
      }
    },
  };
}

/** Per-run tool-call budgets (global + per-tool). Exceeded calls are blocked with feedback or throw. */
export function toolCallLimitMiddleware(opts: {
  toolName?: string;
  runLimit?: number;
  exitBehavior?: "continue" | "error";
}): AgentMiddleware {
  if (opts.runLimit == null) throw new Error("toolCallLimitMiddleware: runLimit is required");
  const counts = new Map<string, number>();
  return {
    name: "tool-call-limit",
    beforeModel: (_req, ctx) => {
      if (ctx.step === 0) counts.clear();
      return undefined;
    },
    beforeTool: (call) => {
      if (opts.toolName && call.name !== opts.toolName) return undefined;
      const key = opts.toolName ?? "*";
      const used = (counts.get(key) ?? 0) + 1;
      counts.set(key, used);
      if (used > (opts.runLimit ?? Number.POSITIVE_INFINITY)) {
        if (opts.exitBehavior === "error") {
          throw new AgentError("BUDGET_EXCEEDED", `Tool call limit exceeded for ${opts.toolName ?? "all tools"} (${opts.runLimit})`, false);
        }
        throw new ToolRejection(`Tool "${call.name}" call limit exceeded (${opts.runLimit}). Do not retry this tool; finish with what you have.`);
      }
      return undefined;
    },
  };
}

export type HitlDecision = "approve" | { edit: unknown } | { reject: string } | { suspend: true };

/** Thrown to suspend a run for human review. Persisted by checkpoint when checkpointer+threadId are set. */
export class HitlInterrupt extends Error {
  readonly interruptId: string;
  readonly tool: string;
  readonly args: unknown;
  constructor(interruptId: string, tool: string, args: unknown) {
    super(`Approval needed for tool "${tool}" (interrupt ${interruptId})`);
    this.name = "HitlInterrupt";
    this.interruptId = interruptId;
    this.tool = tool;
    this.args = args;
  }
}

/** Stable key binding a decision to one tool call (used across suspend/resume). */
export function hitlKey(tool: string, args: unknown): string {
  return `${tool}:${JSON.stringify(args ?? {})}`;
}

export interface HitlDecideRequest {
  name: string;
  args: unknown;
  allowed: ("approve" | "edit" | "reject")[];
  interruptId: string;
}

/**
 * Human-in-the-loop: pause tool execution for approval/edit/reject.
 * Single-process version — `decide` is called inline (CLI prompt, API
 * handler, test stub). Checkpointed resume lands with durable runtime (P3).
 */
export function humanInTheLoopMiddleware(opts: {
  interruptOn: Record<string, boolean | { allowedDecisions?: ("approve" | "edit" | "reject")[] }>;
  /** Inline decider. May return { suspend: true } to checkpoint + throw HitlInterrupt. Omit to always suspend. */
  decide?: (req: HitlDecideRequest) => Promise<HitlDecision> | HitlDecision;
}): AgentMiddleware {
  return {
    name: "human-in-the-loop",
    beforeTool: async (call, ctx) => {
      const rule = opts.interruptOn[call.name];
      if (!rule) return undefined;
      const allowed = rule === true ? (["approve", "edit", "reject"] as const) : (rule.allowedDecisions ?? ["approve", "edit", "reject"]);
      const id = hitlKey(call.name, call.args);
      // Decision keys: exact hitlKey(tool, args) first, then a bare TOOL NAME —
      // the human picks the scope. The wildcard matters for resume: a model
      // re-issuing a call regenerates arg text (temperature), so exact keys
      // never converge; "publish": "approve" approves the re-issued call.
      const all = (ctx.invocationState?.__hitlDecisions as Record<string, HitlDecision> | undefined) ?? {};
      const stored = all[id] ?? all[call.name];
      const decision: HitlDecision = stored ?? (opts.decide ? await opts.decide({ name: call.name, args: call.args, allowed: [...allowed], interruptId: id }) : { suspend: true });
      if (typeof decision === "object" && "suspend" in decision) {
        throw new HitlInterrupt(id, call.name, call.args);
      }
      if (decision === "approve") {
        if (!allowed.includes("approve")) throw new AgentError("HITL_DECISION", `"approve" not allowed for ${call.name}`, false);
        return undefined;
      }
      if (typeof decision === "object" && "edit" in decision) {
        if (!allowed.includes("edit")) throw new AgentError("HITL_DECISION", `"edit" not allowed for ${call.name}`, false);
        return { name: call.name, args: decision.edit };
      }
      const message = (decision as { reject: string }).reject;
      if (!allowed.includes("reject")) throw new AgentError("HITL_DECISION", `"reject" not allowed for ${call.name}`, false);
      throw new ToolRejection(typeof message === "string" && message ? message : `User rejected ${call.name}. Do not retry this tool call.`);
    },
  };
}

// ---------------------------------------------------------------------------
// PII
// ---------------------------------------------------------------------------

export interface PIIMatch {
  text: string;
  start: number;
  end: number;
}

export type PIIDetector = RegExp | string | ((content: string) => PIIMatch[]);
export type PIIStrategy = "block" | "redact" | "mask" | "hash";

const BUILTIN_DETECTORS: Record<string, RegExp> = {
  email: /[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}/g,
  credit_card: /\b(?:\d[ -]?){13,16}\b/g,
  ip: /\b(?:\d{1,3}\.){3}\d{1,3}\b/g,
};

function findMatches(detector: PIIDetector, content: string): PIIMatch[] {
  if (typeof detector === "function") return detector(content);
  const re = typeof detector === "string" ? new RegExp(detector, "g") : new RegExp(detector.source, detector.flags.includes("g") ? detector.flags : `${detector.flags}g`);
  const out: PIIMatch[] = [];
  let m: RegExpExecArray | null;
  while ((m = re.exec(content)) !== null) {
    out.push({ text: m[0], start: m.index, end: m.index + m[0].length });
    if (m[0].length === 0) re.lastIndex += 1;
  }
  return out;
}

function djb2(text: string): string {
  let h = 5381;
  for (let i = 0; i < text.length; i++) h = ((h << 5) + h + text.charCodeAt(i)) >>> 0;
  return h.toString(16).padStart(8, "0");
}

function applyStrategy(text: string, type: string, strategy: PIIStrategy): string {
  switch (strategy) {
    case "redact":
      return `[REDACTED_${type.toUpperCase()}]`;
    case "mask": {
      const visible = text.slice(-4);
      return `${"*".repeat(Math.max(0, text.length - 4))}${visible}`;
    }
    case "hash":
      return `<${type}_hash:${djb2(text)}>`;
    case "block":
      throw new AgentError("PII_BLOCKED", `Blocked ${type} in content`, false);
  }
}

function scrubText(content: string, type: string, detector: PIIDetector, strategy: PIIStrategy): string {
  const matches = findMatches(detector, content).sort((a, b) => b.start - a.start);
  let out = content;
  for (const m of matches) out = out.slice(0, m.start) + applyStrategy(m.text, type, strategy) + out.slice(m.end);
  return out;
}

function scrubBlocks(blocks: ContentBlock[], fn: (text: string) => string): ContentBlock[] {
  return blocks.map((b) => {
    if (b.type === "text") return { ...b, text: fn(b.text) };
    if (b.type === "reasoning") return { ...b, reasoning: fn(b.reasoning) };
    return b;
  });
}

/** PII detection with redact/mask/hash/block strategies across input, output, and tool results. */
export function piiMiddleware(
  piiType: string,
  opts: {
    strategy?: PIIStrategy;
    detector?: PIIDetector;
    applyToInput?: boolean;
    applyToOutput?: boolean;
    applyToToolResults?: boolean;
  } = {},
): AgentMiddleware {
  const strategy = opts.strategy ?? "redact";
  const detector = opts.detector ?? BUILTIN_DETECTORS[piiType] ?? (() => [] as PIIMatch[]);
  const scrub = (s: string) => scrubText(s, piiType, detector, strategy);
  return {
    name: `pii-${piiType}`,
    beforeModel: (req) => {
      if (opts.applyToInput === false) return undefined;
      return {
        ...req,
        messages: req.messages.map((m) =>
          typeof m.content === "string" ? { ...m, content: scrub(m.content) } : { ...m, content: scrubBlocks(m.content, scrub) },
        ),
      };
    },
    afterModel: (res) => {
      if (!opts.applyToOutput) return undefined;
      return { ...res, text: scrub(res.text) };
    },
    afterTool: (_call, output) => {
      if (!opts.applyToToolResults || typeof output !== "string") return output;
      return scrub(output);
    },
  };
}

// ---------------------------------------------------------------------------
// Summarization + context editing
// ---------------------------------------------------------------------------

import { estimateTokens } from "../utils/index.js";
import { textOf } from "../types/index.js";

function messageTokens(m: ChatMessage): number {
  return estimateTokens(typeof m.content === "string" ? m.content : textOf(m.content));
}

/**
 * Auto-summarize history when estimated tokens pass `triggerTokens`,
 * keeping the newest `keepMessages`. Text-oriented: summarized-away
 * multimodal blocks survive only as prose (store media externally and
 * pass URLs if images must persist).
 */
export function summarizationMiddleware(opts: {
  model: ModelProvider;
  triggerTokens: number;
  keepMessages?: number;
  summaryPrefix?: string;
  summarize?: (messages: ChatMessage[]) => Promise<string>;
}): AgentMiddleware {
  const keep = Math.max(1, opts.keepMessages ?? 20);
  return {
    name: "summarization",
    beforeModel: async (req) => {
      const total = req.messages.reduce((n, m) => n + messageTokens(m), 0);
      if (total < opts.triggerTokens) return undefined;
      const compress = req.messages.slice(0, Math.max(0, req.messages.length - keep));
      if (!compress.length) return undefined;
      const summary = opts.summarize
        ? await opts.summarize(compress)
        : await summarizeWithModel(opts.model, compress);
      const prefix = opts.summaryPrefix ?? "Summary of earlier context:";
      return {
        ...req,
        messages: [
          req.messages[0],
          { role: "assistant", content: `${prefix}\n${summary}` },
          ...req.messages.slice(req.messages.length - keep),
        ],
      };
    },
  };
}

async function summarizeWithModel(model: ModelProvider, messages: ChatMessage[]): Promise<string> {
  const transcript = messages.map((m) => `[${m.role}] ${typeof m.content === "string" ? m.content : textOf(m.content)}`).join("\n");
  const res = await model.complete({
    messages: [{ role: "user", content: `Summarize this conversation history concisely, preserving decisions, names, and open items:\n${transcript}` }],
    tools: [],
  });
  return res.text;
}

export interface ClearToolUsesOptions {
  triggerTokens?: number;
  keep?: number;
  clearToolInputs?: boolean;
  excludeTools?: string[];
  placeholder?: string;
}

/** Clear older tool outputs past a token budget; newest `keep` results survive. */
export class ClearToolUsesEdit {
  private opts: Required<ClearToolUsesOptions>;
  constructor(opts: ClearToolUsesOptions = {}) {
    this.opts = {
      triggerTokens: opts.triggerTokens ?? 100000,
      keep: opts.keep ?? 3,
      clearToolInputs: opts.clearToolInputs ?? false,
      excludeTools: opts.excludeTools ?? [],
      placeholder: opts.placeholder ?? "[cleared]",
    };
  }

  apply(messages: ChatMessage[]): ChatMessage[] {
    const total = messages.reduce((n, m) => n + messageTokens(m), 0);
    if (total < this.opts.triggerTokens) return messages;
    const excluded = new Set(this.opts.excludeTools);
    const toolIdx = messages.map((m, i) => (m.role === "tool" && m.toolName && !excluded.has(m.toolName) ? i : -1)).filter((i) => i >= 0);
    const clearable = new Set(toolIdx.slice(0, Math.max(0, toolIdx.length - this.opts.keep)));
    return messages.map((m, i) => {
      if (!clearable.has(i) || m.role !== "tool") return m;
      return { ...m, content: this.opts.placeholder };
    });
  }

  clearInputs(messages: ChatMessage[]): ChatMessage[] {
    if (!this.opts.clearToolInputs) return messages;
    const excluded = new Set(this.opts.excludeTools);
    return messages.map((m) => {
      if (typeof m.content === "string" || m.role !== "assistant") return m;
      return {
        ...m,
        content: m.content.map((b) =>
          b.type === "tool_call" && !excluded.has(b.name) ? { ...b, args: {} } : b,
        ),
      };
    });
  }
}

export function contextEditingMiddleware(opts: { edits?: ClearToolUsesEdit[] }): AgentMiddleware {
  const edits = opts.edits ?? [new ClearToolUsesEdit()];
  return {
    name: "context-editing",
    beforeModel: (req) => {
      let messages = req.messages;
      for (const edit of edits) {
        messages = edit.clearInputs(edit.apply(messages));
      }
      return messages === req.messages ? undefined : { ...req, messages };
    },
  };
}

// ---------------------------------------------------------------------------
// Todo list
// ---------------------------------------------------------------------------

export interface TodoItem {
  content: string;
  status: "pending" | "active" | "completed";
}

/** Task planning: contributes a `write_todos` tool and tracks state per run. */
export function todoListMiddleware(): AgentMiddleware & { listTodos(): TodoItem[] } {
  let todos: TodoItem[] = [];
  return {
    name: "todo-list",
    tools: [
      defineTool({
        name: "write_todos",
        description: "Track multi-step progress. Set exactly one item active at a time; mark completed as you finish.",
        schema: z.object({ todos: z.array(z.object({ content: z.string(), status: z.enum(["pending", "active", "completed"]) })) }),
        execute: async ({ todos: next }: { todos: TodoItem[] }) => {
          todos = next;
          const open = todos.filter((t) => t.status !== "completed").length;
          return `Todos updated (${open} open):\n${todos.map((t) => `- [${t.status === "completed" ? "x" : " "}] ${t.content}`).join("\n")}`;
        },
      }),
    ],
    beforeModel: (req, ctx) => {
      if (ctx.step === 0) todos = [];
      const hinted = req.messages[0]?.content;
      const hint = "For multi-step tasks, plan with write_todos first; keep exactly one item active.";
      if (typeof hinted === "string" && !hinted.includes("write_todos")) {
        return { ...req, messages: [{ ...req.messages[0], content: `${hinted}\n${hint}` }, ...req.messages.slice(1)] };
      }
      return undefined;
    },
    listTodos: () => [...todos],
  };
}

// ---------------------------------------------------------------------------
// LLM tool selector + emulator
// ---------------------------------------------------------------------------

/** Narrow 10+ tools to the relevant few before the main model call. */
export function llmToolSelectorMiddleware(opts: {
  model: ModelProvider;
  maxTools?: number;
  alwaysInclude?: string[];
  systemPrompt?: string;
}): AgentMiddleware {
  return {
    name: "llm-tool-selector",
    beforeModel: async (req) => {
      if (opts.maxTools != null && req.tools.length <= opts.maxTools && !opts.alwaysInclude) return undefined;
      const lastUser = [...req.messages].reverse().find((m) => m.role === "user");
      const menu = req.tools.map((t) => `- ${t.name}: ${t.description}`).join("\n");
      const res = await opts.model.complete({
        messages: [
          { role: "system", content: opts.systemPrompt ?? "Pick relevant tools. Reply with ONLY a comma-separated list of tool names." },
          { role: "user", content: `Tools:\n${menu}\n\nQuery: ${lastUser ? textOf(lastUser.content) : ""}` },
        ],
        tools: [],
      });
      const picked = new Set(res.text.split(",").map((s) => s.trim()).filter(Boolean));
      for (const name of opts.alwaysInclude ?? []) picked.add(name);
      let tools = req.tools.filter((t) => picked.has(t.name));
      if (opts.maxTools != null) tools = tools.slice(0, opts.maxTools);
      if (!tools.length) return undefined;
      return { ...req, tools };
    },
  };
}

/** Emulate tool execution with an LLM — testing/prototyping without side effects. */
export function toolEmulatorMiddleware(opts: { tools?: string[]; model?: ModelProvider } = {}): AgentMiddleware {
  const only = opts.tools ? new Set(opts.tools) : undefined;
  return {
    name: "tool-emulator",
    aroundTool: async (call, next, _ctx) => {
      if ((only && !only.has(call.name)) || !opts.model) {
        if (!opts.model) return `[emulated ${call.name}] no emulator model configured`;
        return next();
      }
      const res = await opts.model.complete({
        messages: [{ role: "user", content: `Emulate tool "${call.name}" with arguments ${JSON.stringify(call.args)}. Reply with ONLY the plausible tool output.` }],
        tools: [],
      });
      return res.text;
    },
  };
}

// ---------------------------------------------------------------------------
// Filesystem tools
// ---------------------------------------------------------------------------

import { z } from "zod";
import { defineTool } from "../tools/index.js";
import { mkdir, readdir, readFile, writeFile } from "node:fs/promises";
import { dirname, resolve } from "node:path";

/** ls/read_file/write_file/edit_file scoped to `root` (escapes rejected). */
export function filesystemTools(root: string): Tool[] {
  const base = resolve(root);
  const resolveIn = (p: string): string => {
    const full = resolve(base, p);
    if (full !== base && !full.startsWith(`${base}/`)) throw new AgentError("FS_ESCAPE", `Path escapes root: ${p}`, false);
    return full;
  };
  return [
    defineTool({
      name: "ls",
      description: "List files in a directory (relative to workspace root).",
      schema: z.object({ path: z.string().optional() }),
      execute: async ({ path }: { path?: string }) => (await readdir(resolveIn(path ?? "."))).join("\n"),
    }),
    defineTool({
      name: "read_file",
      description: "Read a file's full text.",
      schema: z.object({ path: z.string() }),
      execute: async ({ path }: { path: string }) => readFile(resolveIn(path), "utf8"),
    }),
    defineTool({
      name: "write_file",
      description: "Write (create or overwrite) a file.",
      schema: z.object({ path: z.string(), content: z.string() }),
      execute: async ({ path, content }: { path: string; content: string }) => {
        const full = resolveIn(path);
        await mkdir(dirname(full), { recursive: true });
        await writeFile(full, content, "utf8");
        return `wrote ${path} (${content.length} chars)`;
      },
    }),
    defineTool({
      name: "edit_file",
      description: "Replace the first occurrence of oldString with newString in a file.",
      schema: z.object({ path: z.string(), oldString: z.string(), newString: z.string() }),
      execute: async ({ path, oldString, newString: ns }: { path: string; oldString: string; newString: string }) => {
        const full = resolveIn(path);
        const content = await readFile(full, "utf8");
        if (!content.includes(oldString)) throw new AgentError("FS_NO_MATCH", `oldString not found in ${path}`, false);
        await writeFile(full, content.replace(oldString, ns), "utf8");
        return `edited ${path}`;
      },
    }),
  ];
}

export function filesystemMiddleware(opts: { root: string }): AgentMiddleware {
  return { name: "filesystem", tools: filesystemTools(opts.root) };
}

// ---------------------------------------------------------------------------
// Subagents (task tool)
// ---------------------------------------------------------------------------

import type { Agent } from "../agent/index.js";

/**
 * Spawn named subagents via a `task` tool — isolates context so the
 * supervisor stays clean while the subagent goes deep.
 */
export function createSubAgentMiddleware(opts: {
  model: ModelProvider;
  subagents: { name: string; description: string; systemPrompt?: string; tools?: Tool[] }[];
}): AgentMiddleware {
  return {
    name: "subagents",
    tools: [
      defineTool({
        name: "task",
        description: `Delegate to a specialist subagent. Available: ${opts.subagents.map((s) => `${s.name} (${s.description})`).join("; ")}. Returns the subagent's final answer.`,
        schema: z.object({ subagent: z.string(), input: z.string() }),
        execute: async ({ subagent, input }: { subagent: string; input: string }) => {
          const def = opts.subagents.find((s) => s.name === subagent);
          if (!def) throw new AgentError("SUBAGENT_UNKNOWN", `Unknown subagent: ${subagent}`, false);
          const { Agent: AgentClass } = await import("../agent/index.js");
          const sub: Agent = new AgentClass({
            name: def.name,
            description: def.description,
            systemPrompt: def.systemPrompt,
            model: opts.model,
            tools: def.tools ?? [],
          });
          return (await sub.run(input)).text;
        },
      }),
    ],
  };
}
