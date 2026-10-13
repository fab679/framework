import { z } from "zod";
import { AgentError, type AgentResult, type ContentBlock, type TokenUsage } from "../types/index.js";
import type { Agent, RunOptions } from "../agent/index.js";
import { defineTool } from "../tools/index.js";
import type { InvocationState } from "../state/index.js";
import type { StreamEvent } from "../streaming/index.js";
import { runWithBudget } from "../runtime/index.js";

/**
 * Swarm: autonomous handoffs across specialists, no fixed path.
 * Each member gets a `handoff_to_agent` tool; shared context (task,
 * history, contributed knowledge, roster) is rebuilt for every turn.
 * Safety: max handoffs / iterations / timeouts + ping-pong detection.
 */

export interface SwarmOptions {
  agents: Agent[];
  /** First agent to run. Default: agents[0]. Accepts instance or name. */
  entryPoint?: Agent | string;
  maxHandoffs?: number;
  maxIterations?: number;
  executionTimeoutMs?: number;
  nodeTimeoutMs?: number;
  /** Ping-pong detection: window size + min unique agents (0 = disabled). */
  loopDetectionWindow?: number;
  minUniqueAgents?: number;
}

export interface HandoffSignal {
  to: string;
  message: string;
  context?: Record<string, unknown>;
}

export type SwarmStatus = "COMPLETED" | "FAILED";

export interface SwarmResult {
  status: SwarmStatus;
  text: string;
  nodeHistory: string[];
  results: Record<string, AgentResult>;
  iterations: number;
  handoffs: number;
  usage: TokenUsage;
  sharedContext: Record<string, Record<string, unknown>>;
  error?: string;
}

export type SwarmStreamEvent =
  | { type: "node_start"; agent: string }
  | { type: "node_done"; agent: string }
  | { type: "handoff"; from: string; to: string }
  | { type: "agent_event"; agent: string; event: StreamEvent }
  | { type: "result"; result: SwarmResult };

const HANDOFF_TOOL = "handoff_to_agent";

function handoffSignal(to: string, message: string, context?: Record<string, unknown>): AgentError {
  const err = new AgentError("SWARM_HANDOFF", `Handoff to ${to}`, false);
  (err as unknown as { handoff: HandoffSignal }).handoff = { to, message, context };
  return err;
}

export function readHandoffSignal(err: unknown): HandoffSignal | undefined {
  if (err instanceof AgentError && err.code === "SWARM_HANDOFF") {
    return (err as unknown as { handoff?: HandoffSignal }).handoff;
  }
  return undefined;
}

function combineAbort(parent?: AbortSignal, timeoutMs?: number): AbortSignal | undefined {
  if (!parent && !timeoutMs) return undefined;
  const controller = new AbortController();
  if (parent) {
    if (parent.aborted) controller.abort();
    else parent.addEventListener("abort", () => controller.abort(), { once: true });
  }
  if (timeoutMs) {
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    controller.signal.addEventListener("abort", () => clearTimeout(timer), { once: true });
  }
  return controller.signal;
}

export class Swarm {
  private agents: Agent[];
  private entryPoint: Agent;
  private maxHandoffs: number;
  private maxIterations: number;
  private executionTimeoutMs?: number;
  private nodeTimeoutMs?: number;
  private loopWindow: number;
  private minUniqueAgents: number;

  constructor(opts: SwarmOptions) {
    if (!opts.agents.length) throw new AgentError("SWARM_EMPTY", "Swarm needs at least one agent", false);
    this.agents = opts.agents;
    this.entryPoint =
      typeof opts.entryPoint === "string"
        ? (opts.agents.find((a) => a.name === opts.entryPoint) ??
          (() => { throw new AgentError("SWARM_UNKNOWN_ENTRY", `Unknown entry point: ${opts.entryPoint}`, false); })())
        : (opts.entryPoint ?? opts.agents[0]);
    this.maxHandoffs = opts.maxHandoffs ?? 20;
    this.maxIterations = opts.maxIterations ?? 20;
    this.executionTimeoutMs = opts.executionTimeoutMs;
    this.nodeTimeoutMs = opts.nodeTimeoutMs;
    this.loopWindow = opts.loopDetectionWindow ?? 0;
    // Setting a window alone must enable detection — default the uniqueness
    // threshold instead of requiring BOTH knobs (old behavior: window>0 with
    // minUniqueAgents unset silently detected nothing).
    this.minUniqueAgents = opts.minUniqueAgents ?? (this.loopWindow > 0 ? 2 : 0);
  }

  private roster(except?: string): string {
    return this.agents
      .filter((a) => a.name !== except)
      .map((a) => `- ${a.name}${a.description ? `: ${a.description}` : ""}`)
      .join("\n");
  }

  private buildInput(task: string, history: string[], shared: Record<string, Record<string, unknown>>, message: string, current: string): string {
    const knowledge = Object.entries(shared)
      .map(([agent, facts]) => `• ${agent}: ${JSON.stringify(facts)}`)
      .join("\n");
    return [
      `Original task: ${task}`,
      message ? `Handoff message: ${message}` : "",
      history.length ? `Agents so far: ${history.join(" → ")}` : "",
      knowledge ? `Shared knowledge:\n${knowledge}` : "",
      `Available collaborators:\n${this.roster(current)}`,
      `If you need a collaborator, call ${HANDOFF_TOOL} with their name. Otherwise finish with your final answer as text.`,
    ]
      .filter(Boolean)
      .join("\n\n");
  }

  private memberWithHandoff(agent: Agent, shared: Record<string, Record<string, unknown>>): Agent {
    const handoff = defineTool({
      name: HANDOFF_TOOL,
      description: `Hand control to a specialist. Args: to (agent name), message (what you need), context (facts to share as an object).`,
      schema: z.object({ to: z.string(), message: z.string(), context: z.record(z.unknown()).optional() }),
      execute: async ({ to, message, context }: { to: string; message: string; context?: Record<string, unknown> }) => {
        if (!this.agents.some((a) => a.name === to)) {
          return `Error: unknown agent "${to}". Available: ${this.agents.map((a) => a.name).join(", ")}. Pick one or finish yourself.`;
        }
        if (context && Object.keys(context).length) shared[agent.name] = { ...(shared[agent.name] ?? {}), ...context };
        throw handoffSignal(to, message, context);
      },
    });
    // Clone keeps the member's model/tools/middleware; only coordination is added.
    // Memory/sessions are intentionally fresh per handoff turn (shared
    // knowledge flows through the swarm input + invocationState instead).
    return agent.withTools(handoff);
  }

  async run(task: string | ContentBlock[], opts?: RunOptions & { sharedState?: InvocationState }): Promise<SwarmResult> {
    const stream = this.stream(task, opts);
    for (;;) {
      const next = await stream.next();
      if (next.done) return next.value;
    }
  }

  async *stream(
    task: string | ContentBlock[],
    opts?: RunOptions & { sharedState?: InvocationState },
  ): AsyncGenerator<SwarmStreamEvent, SwarmResult> {
    const taskText = typeof task === "string" ? task : task.map((b) => (b.type === "text" ? b.text : `[${b.type}]`)).join("\n");
    const invocationState = opts?.sharedState ?? {};
    const execSignal = combineAbort(opts?.signal, this.executionTimeoutMs);
    const shared: Record<string, Record<string, unknown>> = {};
    const history: string[] = [];
    const results: Record<string, AgentResult> = {};
    const usage: TokenUsage = { inputTokens: 0, outputTokens: 0 };
    const handoffTrail: string[] = [];
    let current = this.entryPoint;
    let message = "";
    let iterations = 0;
    let handoffs = 0;
    let finalText = "";

    const fail = (error: string): SwarmResult =>
      this.finish("FAILED", error, history, results, usage, shared, iterations, handoffs);

    for (;;) {
      iterations += 1;
      if (iterations > this.maxIterations) {
        return fail(`Exceeded maxIterations (${this.maxIterations})`);
      }
      yield { type: "node_start", agent: current.name };
      const runner = this.memberWithHandoff(current, shared);
      const nodeSignal = combineAbort(execSignal, this.nodeTimeoutMs);
      let result: AgentResult | undefined;
      try {
        const gen = runner.stream(this.buildInput(taskText, history, shared, message, current.name), {
          signal: nodeSignal,
          invocationState,
        });
        for (;;) {
          const next = await runWithBudget(() => gen.next(), { signal: execSignal });
          if (next.done) {
            result = next.value;
            break;
          }
          yield { type: "agent_event", agent: current.name, event: next.value };
        }
      } catch (err) {
        const handoff = readHandoffSignal(err);
        if (!handoff) {
          const messageText = err instanceof Error ? err.message : String(err);
          return fail(`Agent ${current.name} failed: ${messageText}`);
        }
        handoffs += 1;
        if (handoffs > this.maxHandoffs) {
          return fail(`Exceeded maxHandoffs (${this.maxHandoffs})`);
        }
        handoffTrail.push(current.name, handoff.to);
        yield { type: "handoff", from: current.name, to: handoff.to };
        const loop = this.detectLoop(handoffTrail);
        if (loop) {
          return fail(loop);
        }
        const next = this.agents.find((a) => a.name === handoff.to);
        if (!next) {
          return fail(`Handoff to unknown agent: ${handoff.to}`);
        }
        history.push(current.name);
        message = handoff.message;
        current = next;
        continue;
      }
      // Completed without handoff → final answer.
      if (!result) return fail(`Agent ${current.name} returned no result`);
      history.push(current.name);
      results[current.name] = result;
      usage.inputTokens += result.usage.inputTokens;
      usage.outputTokens += result.usage.outputTokens;
      usage.reasoningTokens = (usage.reasoningTokens ?? 0) + (result.usage.reasoningTokens ?? 0);
      finalText = result.text;
      yield { type: "node_done", agent: current.name };
      const done = this.finish("COMPLETED", "", history, results, usage, shared, iterations, handoffs, finalText);
      yield { type: "result", result: done };
      return done;
    }
  }

  private detectLoop(trail: string[]): string | undefined {
    if (this.loopWindow <= 0 || this.minUniqueAgents <= 0) return undefined;
    const window = trail.slice(-this.loopWindow);
    if (window.length < this.loopWindow) return undefined;
    if (new Set(window).size < this.minUniqueAgents) {
      return `Repetitive handoff detected (${window.join(" → ")}): fewer than ${this.minUniqueAgents} unique agents in the last ${this.loopWindow} trail entries`;
    }
    return undefined;
  }

  private finish(
    status: SwarmStatus,
    error: string,
    history: string[],
    results: Record<string, AgentResult>,
    usage: TokenUsage,
    shared: Record<string, Record<string, unknown>>,
    iterations: number,
    handoffs: number,
    text = "",
  ): SwarmResult {
    return {
      status,
      text,
      nodeHistory: [...history],
      results,
      iterations,
      handoffs,
      usage,
      sharedContext: shared,
      ...(error ? { error } : {}),
    };
  }
}
