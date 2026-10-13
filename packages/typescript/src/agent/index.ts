import type { StreamEvent } from "../streaming/index.js";
import { ToolRejection, HitlInterrupt, type AgentMiddleware, type HookContext, type HitlDecision, type ModelRequest } from "../middleware/index.js";
import { NullConversationManager, type ConversationManager } from "../conversation/index.js";
import { AgentState, type InvocationState } from "../state/index.js";
import type { Checkpointer } from "../checkpoint/index.js";
import {
  selectSnapshotFields,
  type SessionManager,
  type Snapshot,
  type TakeSnapshotOptions,
} from "../sessions/index.js";
import type { z } from "zod";
import {
  parseStructured,
  responseFormatJsonSchema,
  responseFormatName,
  type ResponseFormat,
} from "../structured/index.js";
import {
  AgentError,
  type AgentResult,
  type ChatMessage,
  type ContentBlock,
  type TokenUsage,
} from "../types/index.js";
import { defaultToResult, defineTool, type Tool } from "../tools/index.js";
import { zodToJsonSchema } from "zod-to-json-schema";
import type { Memory } from "../memory/index.js";
import { InMemoryMemory } from "../memory/index.js";
import type { ModelProvider } from "../models/index.js";
import { priceFor, usageCost, type ModelPrice } from "../models/pricing.js";
import type { AgentEvents } from "../observability/index.js";
import { runWithBudget } from "../runtime/index.js";

export type OutputVersion = "v0" | "v1";

export interface AgentOptions {
  name?: string;
  /** One-line specialty, used by the router menu. */
  description?: string;
  systemPrompt?: string;
  model: ModelProvider;
  tools?: Tool[];
  memory?: Memory;
  maxSteps?: number;
  /** Max tool executions per run; exceeded -> AgentError(BUDGET_EXCEEDED). Default: unlimited. */
  maxToolCalls?: number;
  /** Token/cost/deadline budgets enforced after every model call. */
  budgets?: TokenBudgets;
  /**
   * Output version. v0: assistant messages carry plain text (thinking streams
   * via events only). v1: thinking is also stored as `reasoning` blocks.
   */
  outputVersion?: OutputVersion;
  /** Typed final answer (tool or provider strategy). Sets `structuredResponse` on the result. */
  responseFormat?: ResponseFormat;
  /** Middleware hooks (before/after model + tool). Run in order. */
  middleware?: AgentMiddleware[];
  /** Conversation history: seed prior messages to continue a conversation. */
  initialMessages?: ChatMessage[];
  /** Agent key-value state (never sent to the model). */
  state?: Record<string, unknown> | AgentState;
  /** History management. Default: NullConversationManager (keep everything; memory still windows). */
  conversationManager?: ConversationManager;
  /** Persist state + history across restarts. Restored on first run, saved after each run. */
  sessionManager?: SessionManager;
  events?: AgentEvents;
}

export interface TimelineEntry {
  rev: number;
  kind: "run" | "edit" | "branch" | "resume" | "snapshot";
  label?: string;
  at: string;
}

export interface TokenBudgets {
  /** Total (input + output + reasoning) tokens per run. */
  maxTokens?: number;
  /** USD cap per run. Needs a known price (table lookup or `prices`) — never guessed. */
  maxCostUsd?: number;
  /** Price override when the model is missing from the pricing table. */
  prices?: ModelPrice;
  /** Wall-clock deadline per run. */
  deadlineMs?: number;
}

export interface RunOptions {
  signal?: AbortSignal;
  /** Per-run data: shared by reference across hooks/tools, returned on the result, never sent to the model. */
  invocationState?: InvocationState;
  /** Checkpointed HITL: persist + resume human approvals locally. */
  checkpointer?: Checkpointer;
  threadId?: string;
}

const DEFAULT_SYSTEM = "You are a helpful agent. Use tools when needed. Be concise.";
const DEFAULT_MAX_STEPS = 6;

/** Tool metadata sent to the model — descriptions alone leave arg shapes to guesswork. */
const toolJsonSchemas = new WeakMap<Tool, Record<string, unknown> | undefined>();
function toolParameters(t: Tool): Record<string, unknown> | undefined {
  if (!toolJsonSchemas.has(t)) {
    try {
      toolJsonSchemas.set(t, zodToJsonSchema(t.schema as never, { $refStrategy: "none" }) as Record<string, unknown>);
    } catch {
      toolJsonSchemas.set(t, undefined);
    }
  }
  return toolJsonSchemas.get(t);
}

interface LoopRuntime {
  model: ModelProvider;
  memory?: Memory;
  systemPrompt: string;
  events?: AgentEvents;
  middleware: AgentMiddleware[];
  conversationManager: ConversationManager;
  sessionManager?: SessionManager;
  tools: Tool[];
  format?: ResponseFormat;
  strategy?: string;
  maxSteps: number;
  maxToolCalls?: number;
  budgets?: TokenBudgets;
  outputVersion: OutputVersion;
  state: AgentState;
  toolCtx: { agentName: string; state: AgentState; invocationState: InvocationState };
  ctxFor: (step: number) => HookContext;
}

/**
 * Minimal ReAct-style loop:
 * 1. load memory -> messages
 * 2. loop: model.complete -> tool calls -> execute -> append
 * 3. save memory, return AgentResult
 *
 * `run()` drains `stream()`; both fire `events` hooks identically.
 */
export class Agent {
  private options: Required<Omit<AgentOptions, "events" | "tools" | "memory" | "maxToolCalls" | "budgets" | "responseFormat" | "middleware" | "initialMessages" | "state" | "conversationManager" | "sessionManager">> & {
    tools: Tool[];
    memory?: Memory;
    maxToolCalls?: number;
    budgets?: TokenBudgets;
    responseFormat?: ResponseFormat;
    middleware: AgentMiddleware[];
    initialMessages: ChatMessage[];
    conversationManager: ConversationManager;
    sessionManager?: SessionManager;
    events?: AgentEvents;
  };
  private seeded = false;
  private restored = false;
  private lastMessages: ChatMessage[] = [];
  private revisions: TimelineEntry[] = [];
  readonly agentState: AgentState;
  private instanceSessionId?: string;

  constructor(options: AgentOptions) {
    this.options = {
      name: options.name ?? "agent",
      description: options.description ?? "",
      systemPrompt: options.systemPrompt ?? DEFAULT_SYSTEM,
      model: options.model,
      // Middleware-contributed tools (filesystem, todos, subagents) come first.
      // withTools()/branch() pass an already-merged list back in, so dedupe
      // by name (first occurrence wins) to keep clones from advertising
      // every middleware tool twice.
      tools: [...(options.middleware ?? []).flatMap((m) => m.tools ?? []), ...(options.tools ?? [])]
        .filter((t, i, all) => all.findIndex((o) => o.name === t.name) === i),
      memory: options.memory,
      maxSteps: options.maxSteps ?? DEFAULT_MAX_STEPS,
      maxToolCalls: options.maxToolCalls,
      budgets: options.budgets,
      outputVersion: options.outputVersion ?? "v0",
      responseFormat: options.responseFormat,
      middleware: options.middleware ?? [],
      initialMessages: options.initialMessages ?? [],
      conversationManager: options.conversationManager ?? new NullConversationManager(),
      sessionManager: options.sessionManager,
      events: options.events,
    };
    this.agentState = options.state instanceof AgentState ? options.state : new AgentState(options.state ?? {});
  }

  /** Public identity for orchestration menus and logs. */
  get name(): string {
    return this.options.name;
  }

  get description(): string {
    return this.options.description;
  }

  get model(): ModelProvider {
    return this.options.model;
  }

  /** Member tools (copy) — introspection for orchestration/menus. */
  get tools(): Tool[] {
    return [...this.options.tools];
  }

  /**
   * Clone with extra tools. Memory, session, and state are NOT carried over
   * (fresh per clone); model, tools, middleware, and formats are shared.
   * Used by coordinators (swarm handoffs) that inject control tools.
   */
  withTools(...extra: Tool[]): Agent {
    return new Agent({
      name: this.options.name,
      description: this.options.description,
      systemPrompt: this.options.systemPrompt,
      model: this.options.model,
      tools: [...this.options.tools, ...extra],
      maxSteps: this.options.maxSteps,
      maxToolCalls: this.options.maxToolCalls,
      budgets: this.options.budgets,
      outputVersion: this.options.outputVersion,
      responseFormat: this.options.responseFormat,
      middleware: [...this.options.middleware],
      conversationManager: this.options.conversationManager,
      events: this.options.events,
    });
  }

  /** Key-value state (never sent to the model). */
  get state(): AgentState {
    return this.agentState;
  }

  /** Session handle: the manager's id, or a generated per-instance id. */
  get sessionId(): string {
    if (this.options.sessionManager) return this.options.sessionManager.sessionId;
    this.instanceSessionId ??= Math.random().toString(16).slice(2, 10);
    return this.instanceSessionId;
  }

  /** Full conversation history from the last run (system + memory + new turns). */
  get messages(): ChatMessage[] {
    return [...this.lastMessages];
  }

  /** Version timeline: runs, edits, branches, resumes (append-only). */
  get timeline(): TimelineEntry[] {
    return [...this.revisions];
  }

  private record(kind: TimelineEntry["kind"], label?: string): void {
    this.revisions.push({ rev: this.revisions.length + 1, kind, ...(label ? { label } : {}), at: new Date().toISOString() });
  }

  /**
   * Edit a message in the current history (by id or index) and keep the
   * version in the timeline. History + memory stay in sync.
   */
  async editMessage(idOrIndex: string | number, content: string | ContentBlock[]): Promise<ChatMessage> {
    const idx = typeof idOrIndex === "number"
      ? idOrIndex
      : this.lastMessages.findIndex((m) => m.id === idOrIndex);
    if (idx < 0 || idx >= this.lastMessages.length) {
      throw new AgentError("MESSAGE_NOT_FOUND", `No message ${String(idOrIndex)} in history`, false);
    }
    // The following role:"tool" messages reference this turn's tool_call ids;
    // replacing the content would orphan them (providers 400 on unpaired tool results).
    if (
      typeof this.lastMessages[idx].content !== "string" &&
      this.lastMessages[idx].content.some((b) => b.type === "tool_call")
    ) {
      throw new AgentError("MESSAGE_NOT_EDITABLE", `Message ${String(idOrIndex)} contains tool calls; branch() from a point before it instead`, false);
    }
    const updated = { ...this.lastMessages[idx], content };
    this.lastMessages = [...this.lastMessages.slice(0, idx), updated, ...this.lastMessages.slice(idx + 1)];
    if (this.options.memory) {
      await this.options.memory.clear();
      await this.options.memory.save(this.lastMessages.filter((m) => m.role !== "system"));
    }
    this.record("edit", `message ${updated.id ?? idx}`);
    return updated;
  }

  /**
   * Branch the current history into an independent agent. Model, tools,
   * middleware, and formats are shared; state is copied; memory is fresh
   * (seeded with this history); sessions are NOT carried over (to avoid
   * double-persist collisions). Both timelines record the branch.
   */
  async branch(label: string, overrides: Partial<AgentOptions> = {}): Promise<Agent> {
    const memory = new InMemoryMemory();
    await memory.save(this.lastMessages.filter((m) => m.role !== "system"));
    const child = new Agent({
      name: this.options.name,
      description: this.options.description,
      systemPrompt: this.options.systemPrompt,
      model: this.options.model,
      tools: [...this.options.tools],
      memory,
      maxSteps: this.options.maxSteps,
      maxToolCalls: this.options.maxToolCalls,
      budgets: this.options.budgets,
      outputVersion: this.options.outputVersion,
      responseFormat: this.options.responseFormat,
      middleware: [...this.options.middleware],
      state: this.agentState.toJSON(),
      conversationManager: this.options.conversationManager,
      events: this.options.events,
      ...overrides,
    });
    child.lastMessages = [...this.lastMessages];
    child.revisions = [...this.revisions];
    child.record("branch", label);
    this.record("branch", label);
    return child;
  }

  /**
   * Pause mid-conversation: persist history + state under threadId without
   * needing an interrupt. Pair with resume() (empty decisions continue).
   */
  async pause(
    threadId: string,
    opts: { checkpointer: Checkpointer },
  ): Promise<{ threadId: string; messages: number }> {
    await opts.checkpointer.save(threadId, {
      messages: [...this.lastMessages],
      state: this.agentState.toJSON(),
      pending: [],
    });
    return { threadId, messages: this.lastMessages.length };
  }

  /** Manual checkpoint: messages + state (+ system prompt on request) + appData. */
  takeSnapshot(opts: TakeSnapshotOptions = { preset: "session" }): Snapshot {
    return {
      id: `snapshot-${Date.now().toString(36)}`,
      createdAt: new Date().toISOString(),
      data: selectSnapshotFields(
        { messages: [...this.lastMessages], state: this.agentState.toJSON(), systemPrompt: this.options.systemPrompt },
        opts,
      ),
      ...(opts.appData ? { appData: opts.appData } : {}),
    };
  }

  /** Restore fields present in the snapshot; absent fields keep current values. */
  async loadSnapshot(snapshot: Snapshot): Promise<void> {
    if (snapshot.data.state) {
      for (const [k, v] of Object.entries(snapshot.data.state)) this.agentState.set(k, v);
    }
    if (snapshot.data.messages) {
      this.lastMessages = [...snapshot.data.messages];
      if (this.options.memory) {
        await this.options.memory.clear();
        await this.options.memory.save(snapshot.data.messages.filter((m) => m.role !== "system"));
      }
    }
  }

  /**
   * Resume a suspended run: restores the checkpoint, seeds human decisions,
   * and continues the loop without new input. Decisions are keyed by
   * `hitlKey(tool, args)` for an exact match, or by bare tool name to cover
   * every re-issued call of that tool (models re-issue with regenerated arg
   * text, so exact keys alone would never converge).
   */
  async resume(
    threadId: string,
    decisions: Record<string, HitlDecision> = {},
    opts: { checkpointer: Checkpointer } & RunOptions,
  ): Promise<AgentResult> {
    const cp = await opts.checkpointer.load(threadId);
    if (!cp) throw new AgentError("CHECKPOINT_MISSING", `No checkpoint for thread "${threadId}"`, false);
    if (cp.state) {
      for (const [k, v] of Object.entries(cp.state)) this.agentState.set(k, v);
    }
    this.lastMessages = [...cp.messages];
    if (this.options.memory) {
      await this.options.memory.clear();
      await this.options.memory.save(cp.messages.filter((m) => m.role !== "system"));
    }
    const invocationState = { ...(opts.invocationState ?? {}), __hitlDecisions: decisions };
    const gen = this.iterateContinuation(opts.signal, invocationState, { checkpointer: opts.checkpointer, threadId });
    for (;;) {
      const next = await runWithBudget(() => gen.next(), { signal: opts.signal });
      if (next.done) {
        this.record("resume", threadId);
        return next.value;
      }
    }
  }

  private buildRuntime(invocationState: InvocationState): LoopRuntime {
    const { model, memory, systemPrompt, events, middleware, conversationManager, sessionManager } = this.options;
    const format = this.options.responseFormat;
    // "auto" (and unset) resolves by capability: native response_format when
    // the provider supports it, else the synthetic tool.
    const strategy = format
      ? format.strategy && format.strategy !== "auto"
        ? format.strategy
        : model.capabilities?.structuredOutput ? "provider" : "tool"
      : undefined;
    const tools = strategy === "tool" && format ? [...this.options.tools, structuredTool(format)] : this.options.tools;
    const toolCtx = { agentName: this.options.name, state: this.agentState, invocationState };
    const ctxFor = (step: number): HookContext => ({ step, invocationState });
    return {
      model, memory, systemPrompt, events, middleware, conversationManager,
      sessionManager, tools, format, strategy,
      maxSteps: this.options.maxSteps,
      maxToolCalls: this.options.maxToolCalls,
      budgets: this.options.budgets,
      outputVersion: this.options.outputVersion,
      state: this.agentState, toolCtx, ctxFor,
    };
  }

  /** Continue the loop from restored history (no new user message). */
  private async *iterateContinuation(
    signal: AbortSignal | undefined,
    invocationState: InvocationState,
    checkpoint?: { checkpointer: Checkpointer; threadId: string },
  ): AsyncGenerator<StreamEvent, AgentResult> {
    const rt = this.buildRuntime(invocationState);
    const messages: ChatMessage[] = [
      { role: "system", content: rt.systemPrompt },
      ...(rt.memory ? await rt.memory.load() : this.lastMessages.filter((m) => m.role !== "system")),
    ];
    return yield* this.runLoop(messages, rt, signal, invocationState, checkpoint);
  }

  async run(input: string | ContentBlock[], signal?: AbortSignal): Promise<AgentResult>;
  async run(input: string | ContentBlock[], opts?: RunOptions): Promise<AgentResult>;
  async run(input: string | ContentBlock[], signalOrOpts?: AbortSignal | RunOptions): Promise<AgentResult> {
    const opts: RunOptions = signalOrOpts instanceof AbortSignal ? { signal: signalOrOpts } : (signalOrOpts ?? {});
    const stream = this.stream(input, opts);
    for (;;) {
      const next = await stream.next();
      if (next.done) {
        this.record("run", typeof input === "string" ? input.slice(0, 60) : "blocks");
        return next.value;
      }
    }
  }

  stream(input: string | ContentBlock[], signal?: AbortSignal, invocationState?: InvocationState): AsyncGenerator<StreamEvent, AgentResult>;
  stream(input: string | ContentBlock[], opts?: RunOptions): AsyncGenerator<StreamEvent, AgentResult>;
  async *stream(
    input: string | ContentBlock[],
    signalOrOpts?: AbortSignal | RunOptions,
    invocationState?: InvocationState,
  ): AsyncGenerator<StreamEvent, AgentResult> {
    const opts: RunOptions =
      signalOrOpts instanceof AbortSignal ? { signal: signalOrOpts, invocationState } : (signalOrOpts ?? {});
    const checkpoint =
      opts.checkpointer && opts.threadId ? { checkpointer: opts.checkpointer, threadId: opts.threadId } : undefined;
    const gen = this.iterate(input, opts.signal, opts.invocationState ?? {}, checkpoint);
    for (;;) {
      const next = await runWithBudget(() => gen.next(), { signal: opts.signal });
      if (next.done) return next.value;
      yield next.value;
    }
  }

  private async *iterate(
    input: string | ContentBlock[],
    signal?: AbortSignal,
    invocationState: InvocationState = {},
    checkpoint?: { checkpointer: Checkpointer; threadId: string },
  ): AsyncGenerator<StreamEvent, AgentResult> {
    const rt = this.buildRuntime(invocationState);
    const { memory, systemPrompt } = this.options;

    // Session restore wins over initialMessages: a persisted conversation
    // must not be silently replaced by the seed on the first run.
    const sessionManager = this.options.sessionManager;
    if (sessionManager && !this.restored) {
      this.restored = true;
      const latest = await sessionManager.loadLatest();
      if (latest?.state) {
        for (const [k, v] of Object.entries(latest.state)) this.agentState.set(k, v);
      }
      if (latest?.messages?.length) {
        this.lastMessages = [...latest.messages];
        if (memory && (await memory.load()).length === 0) {
          await memory.save(latest.messages.filter((m) => m.role !== "system"));
        }
      }
    }

    if (!this.seeded && this.options.initialMessages.length > 0) {
      this.seeded = true;
      if (memory && (await memory.load()).length === 0) {
        await memory.save(this.options.initialMessages.filter((m) => m.role !== "system"));
      } else if (!memory && this.lastMessages.length === 0) {
        this.lastMessages = [...this.options.initialMessages];
      }
    }

    const messages: ChatMessage[] = [
      { role: "system", content: systemPrompt },
      ...(memory ? await memory.load() : this.lastMessages.filter((m) => m.role !== "system")),
      { role: "user", content: input },
    ];
    return yield* this.runLoop(messages, rt, signal, invocationState, checkpoint);
  }

  private async *runLoop(
    messages: ChatMessage[],
    rt: LoopRuntime,
    signal: AbortSignal | undefined,
    invocationState: InvocationState,
    checkpoint?: { checkpointer: Checkpointer; threadId: string },
  ): AsyncGenerator<StreamEvent, AgentResult> {
    const { model, memory, systemPrompt, events, middleware, conversationManager, sessionManager, tools, format, strategy, toolCtx, ctxFor } = rt;
    const toolCalls: AgentResult["toolCalls"] = [];
    const usage: TokenUsage = { inputTokens: 0, outputTokens: 0 };
    const startedAt = Date.now();
    let text = "";
    let structuredResponse: unknown;
    let hasStructured = false;

    try {
    for (let step = 0; step < rt.maxSteps; step++) {
      signal?.throwIfAborted();
      if (conversationManager.beforeModel) {
        const managed = await conversationManager.beforeModel(messages, step);
        // Managers may hand back the input array itself (no-op turn) —
        // resetting in place would wipe the very array we spread back in.
        if (managed !== messages) {
          messages.length = 0;
          messages.push(...managed);
        }
      }
      events?.onStep?.({ step, messages });
      yield { type: "step", step };

      let req: ModelRequest = {
        messages,
        tools: tools.map((t) => ({ name: t.name, description: t.description, ...(toolParameters(t) ? { parametersSchema: toolParameters(t) } : {}) })),
        ...(strategy === "provider" && format
          ? { responseFormat: { name: responseFormatName(format), schema: responseFormatJsonSchema(format) } }
          : {}),
      };
      for (const mw of middleware) req = (await mw.beforeModel?.(req, ctxFor(step))) ?? req;

      const pending: ({ type: "token"; delta: string } | { type: "thinking"; delta: string })[] = [];
      const callModel = (r: ModelRequest) =>
        model.complete({
          ...r,
          // Propagate the run's abort into the in-flight provider request.
          ...(signal ? { signal } : {}),
          onToken: (d) => {
            events?.onToken?.(d);
            pending.push({ type: "token", delta: d });
          },
          onThinking: (d) => {
            events?.onThinking?.(d);
            pending.push({ type: "thinking", delta: d });
          },
        });
      const modelChain = middleware
        .filter((mw) => mw.wrapModel)
        .reduceRight(
          (next, mw) => (r: ModelRequest) => mw.wrapModel!(r, next, ctxFor(step)),
          callModel,
        );
      let completion = await modelChain(req);
      for (const e of pending.splice(0)) {
        if (e.delta !== "") yield e;
      }
      for (const mw of [...middleware].reverse()) completion = (await mw.afterModel?.(completion, req, ctxFor(step))) ?? completion;

      usage.inputTokens += completion.usage.inputTokens;
      usage.outputTokens += completion.usage.outputTokens;
      usage.reasoningTokens = (usage.reasoningTokens ?? 0) + (completion.usage.reasoningTokens ?? 0);
      checkBudgets(rt, usage, startedAt, model.id);

      if (completion.toolCalls.length === 0) {
        text = completion.text;
        if (strategy === "provider" && format) {
          try {
            structuredResponse = parseStructured(format, JSON.parse(text));
            hasStructured = true;
          } catch (err) {
            const retryable = err instanceof AgentError ? err.retryable : true;
            const message = err instanceof Error ? err.message : String(err);
            if (step < rt.maxSteps - 1 && retryable) {
              messages.push({ role: "assistant", content: text });
              messages.push({
                role: "user",
                content: `Your last response was not valid for "${responseFormatName(format)}": ${message}. Reply with corrected JSON only.`,
              });
              continue;
            }
            throw err instanceof AgentError
              ? err
              : new AgentError("STRUCTURED_INVALID", `Invalid structured output: ${message}`, true);
          }
        }
        messages.push(
          rt.outputVersion === "v1" && completion.thinking
            ? {
                role: "assistant",
                content: [
                  { type: "reasoning", reasoning: completion.thinking },
                  { type: "text", text },
                ],
              }
            : { role: "assistant", content: text },
        );
        break;
      }

      // Record the model's tool calls as an assistant message so the
      // history round-trips (tool messages reference these ids).
      messages.push({
        role: "assistant",
        content: completion.toolCalls.map((c) => ({ type: "tool_call", id: c.id, name: c.name, args: c.args }) as const),
      });

      for (const call of completion.toolCalls) {
        if (rt.maxToolCalls != null && toolCalls.length >= rt.maxToolCalls) {
          throw new AgentError(
            "BUDGET_EXCEEDED",
            `Exceeded maxToolCalls (${rt.maxToolCalls})`,
            false,
          );
        }
        const tool = tools.find((t) => t.name === call.name);
        if (!tool) {
          messages.push({
            role: "tool",
            content: `Unknown tool: ${call.name}`,
            toolCallId: call.id,
            toolName: call.name,
          });
          continue;
        }
        let toolCall = { name: call.name, args: call.args };
        let output: unknown;
        try {
          for (const mw of middleware) toolCall = (await mw.beforeTool?.(toolCall, ctxFor(step))) ?? toolCall;
          events?.onToolCall?.({ tool: toolCall.name, args: toolCall.args });
          yield { type: "tool_call", name: toolCall.name, args: toolCall.args };

          const invoke = () => tool.execute(toolCall.args, toolCtx);
          const chain = middleware
            .filter((mw) => mw.aroundTool)
            .reduceRight(
              (next, mw) => () => mw.aroundTool!(toolCall, next, ctxFor(step)),
              invoke,
            );
          output = await chain();
        } catch (err) {
          if (err instanceof HitlInterrupt) throw err;
          if (err instanceof ToolRejection) {
            output = err.feedback;
          } else {
            if (err instanceof AgentError) throw err;
            throw new AgentError(
              "TOOL_EXECUTION",
              `Tool "${toolCall.name}" failed: ${err instanceof Error ? err.message : String(err)}`,
              false,
            );
          }
        }
        for (const mw of [...middleware].reverse()) output = (await mw.afterTool?.(toolCall, output, ctxFor(step))) ?? output;

        const result = (tool.toResult ?? defaultToResult)(output);
        toolCalls.push({ id: call.id, name: toolCall.name, args: toolCall.args, output, ...(result.artifact !== undefined ? { artifact: result.artifact } : {}) });
        if (strategy === "tool" && format && toolCall.name === responseFormatName(format) && !hasStructured) {
          try {
            structuredResponse = parseStructured(format, output);
            hasStructured = true;
          } catch {
            // execute already returned a retry message; keep looping.
          }
        }
        events?.onToolResult?.({ tool: toolCall.name, output });
        yield { type: "tool_result", name: toolCall.name, output };
        messages.push({
          role: "tool",
          content: result.content,
          toolCallId: call.id,
          toolName: toolCall.name,
        });
      }

      // If model never produces final text within budget, synthesize from tool outputs.
      if (step === rt.maxSteps - 1) {
        text = toolCalls.length
          ? `Completed ${toolCalls.length} tool call(s). Last output: ${JSON.stringify(toolCalls.at(-1)?.output)}`
          : "";
        // Store it — otherwise history ends on a dangling tool message and
        // the "answer" the caller saw exists nowhere in the transcript.
        messages.push({ role: "assistant", content: text });
      }
    }
    } catch (err) {
      if (err instanceof HitlInterrupt && checkpoint) {
        const pendingArgs = JSON.stringify(err.args ?? {});
        let cut = messages.length;
        for (let i = messages.length - 1; i >= 0; i--) {
          const m = messages[i];
          if (
            m.role === "assistant" &&
            typeof m.content !== "string" &&
            m.content.some((b) => b.type === "tool_call" && b.name === err.tool && JSON.stringify(b.args ?? {}) === pendingArgs)
          ) {
            cut = i;
            break;
          }
        }
        await checkpoint.checkpointer.save(checkpoint.threadId, {
          messages: messages.slice(0, cut),
          state: rt.state.toJSON(),
          pending: [{ interruptId: err.interruptId, tool: err.tool, args: err.args }],
        });
      }
      throw err;
    }

    if (format && !hasStructured) {
      throw new AgentError(
        "STRUCTURED_NO_MATCH",
        `No valid "${responseFormatName(format)}" produced within ${rt.maxSteps} steps`,
        false,
      );
    }

    const managed = await conversationManager.apply(messages);
    // save() APPENDS: without the clear, every run re-saves the entire
    // (already loaded) transcript and duplicates it in memory.
    if (memory) {
      await memory.clear();
      await memory.save(managed.filter((m) => m.role !== "system"));
    }
    this.lastMessages = managed;
    if (sessionManager) {
      await sessionManager.saveLatest({
        messages: managed,
        state: rt.state.toJSON(),
        systemPrompt,
      });
    }
    events?.onDone?.({ text, toolCalls });
    yield { type: "done", text };
    // Known price -> report real cost; unknown stays undefined (never guessed).
    const costUsd = usageCost(usage, model.id, rt.budgets?.prices);
    return {
      text, messages: [...managed], toolCalls,
      usage: { ...usage, ...(costUsd !== undefined ? { costUsd } : {}) },
      invocationState,
      ...(hasStructured ? { structuredResponse } : {}),
    };
  }
}

/** Enforce token/cost/deadline budgets after every model call. */
function checkBudgets(
  rt: { budgets?: TokenBudgets },
  usage: TokenUsage,
  startedAt: number,
  modelId: string,
): void {
  const budgets = rt.budgets;
  if (!budgets) return;
  // reasoningTokens is a SUBSET of outputTokens on every supported provider,
  // so it must not be added on top (double-count would fire budgets early).
  const total = usage.inputTokens + usage.outputTokens;
  if (budgets.maxTokens != null && total > budgets.maxTokens) {
    throw new AgentError("BUDGET_EXCEEDED", `Exceeded maxTokens (${budgets.maxTokens}, used ${total})`, false);
  }
  if (budgets.maxCostUsd != null) {
    const prices = budgets.prices ?? priceFor(modelId);
    if (!prices) {
      throw new AgentError("BUDGET_NO_PRICE", `Cannot enforce maxCostUsd: no price for model "${modelId}" (set budgets.prices)`, false);
    }
    const cost = usageCost(usage, modelId, prices);
    if (cost != null && cost > budgets.maxCostUsd) {
      throw new AgentError("BUDGET_EXCEEDED", `Exceeded maxCostUsd ($${budgets.maxCostUsd}, used $${cost.toFixed(4)})`, false);
    }
  }
  if (budgets.deadlineMs != null && Date.now() - startedAt > budgets.deadlineMs) {
    throw new AgentError("BUDGET_EXCEEDED", `Exceeded deadline (${budgets.deadlineMs}ms)`, false);
  }
}

/** Synthetic tool for the tool strategy: valid output passes, invalid returns a retry message. */
function structuredTool(format: ResponseFormat): Tool {  const name = responseFormatName(format);
  const maxAttempts = format.maxAttempts ?? 2;
  let attempts = 0;
  return defineTool({
    name,
    description: `Return the final answer as structured "${name}". Call exactly once with valid arguments.`,
    schema: format.schema,
    execute: async (args: unknown) => {
      attempts += 1;
      const parsed = (format.schema as z.ZodType<unknown>).safeParse(args);
      if (parsed.success) return parsed.data;
      if (attempts >= maxAttempts) {
        throw new AgentError("STRUCTURED_NO_MATCH", `Invalid "${name}" after ${attempts} attempts: ${parsed.error.message}`, false);
      }
      return `Error: invalid "${name}": ${parsed.error.message}. Fix the arguments and call again.`;
    },
  });
}
