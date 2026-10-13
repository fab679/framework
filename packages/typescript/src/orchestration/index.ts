import { AgentError, type AgentResult } from "../types/index.js";
import type { Agent, RunOptions } from "../agent/index.js";
import type { InvocationState } from "../state/index.js";
import type { ModelProvider } from "../models/index.js";
import type { AgentEvents } from "../observability/index.js";

export type OrchestrationStrategy = "sequential" | "parallel" | "router";

export interface OrchestrationPlan {
  strategy: OrchestrationStrategy;
  agents: Agent[];
  /** Max concurrent agents for parallel. Default: all at once. */
  concurrency?: number;
  /** Model that picks the agent for router. Defaults to the first agent's model. */
  routerModel?: ModelProvider;
  events?: AgentEvents;
}

export interface OrchestratedResult {
  strategy: OrchestrationStrategy;
  results: AgentResult[];
  /** Combined text: last result (sequential/router), joined (parallel). */
  text: string;
  /** Router only: index of the chosen agent. */
  routedTo?: number;
}

function requireAgents(plan: OrchestrationPlan): void {
  if (!plan.agents.length) throw new AgentError("ORCHESTRATION_EMPTY", "Orchestration needs at least one agent", false);
}

/** Chain: each agent receives the previous agent's text appended to the input. */
async function runSequential(
  plan: OrchestrationPlan,
  input: string,
  opts: { signal?: AbortSignal; invocationState?: InvocationState } = {},
): Promise<OrchestratedResult> {
  requireAgents(plan);
  const results: AgentResult[] = [];
  let current = input;
  for (let i = 0; i < plan.agents.length; i++) {
    opts.signal?.throwIfAborted();
    plan.events?.onStep?.({ step: i, messages: [] });
    const res = await plan.agents[i].run(current, { signal: opts.signal, invocationState: opts.invocationState });
    results.push(res);
    current = `${input}\n\nPrevious result (${plan.agents[i].name}):\n${res.text}`;
  }
  return { strategy: "sequential", results, text: results.at(-1)?.text ?? "" };
}

/** Fan-out with a concurrency cap; results keep agent order. */
async function runParallel(
  plan: OrchestrationPlan,
  input: string,
  opts: { signal?: AbortSignal; invocationState?: InvocationState } = {},
): Promise<OrchestratedResult> {
  requireAgents(plan);
  const limit = Math.max(1, plan.concurrency ?? plan.agents.length);
  const results: AgentResult[] = new Array(plan.agents.length);
  let next = 0;
  const workers = Array.from({ length: Math.min(limit, plan.agents.length) }, async () => {
    for (;;) {
      opts.signal?.throwIfAborted();
      const i = next++;
      if (i >= plan.agents.length) return;
      results[i] = await plan.agents[i].run(input, { signal: opts.signal, invocationState: opts.invocationState });
    }
  });
  await Promise.all(workers);
  return { strategy: "parallel", results, text: results.map((r) => r.text).join("\n\n") };
}

/** One model call picks the agent by index; only that agent runs. */
async function runRouter(
  plan: OrchestrationPlan,
  input: string,
  opts: { signal?: AbortSignal; invocationState?: InvocationState } = {},
): Promise<OrchestratedResult> {
  requireAgents(plan);
  const routerModel = plan.routerModel ?? plan.agents[0].model;
  const menu = plan.agents.map((a, i) => `${i}: ${a.name}${a.description ? ` — ${a.description}` : ""}`).join("\n");
  const pick = await routerModel.complete({
    messages: [
      { role: "system", content: "You route user requests to specialist agents. Reply with ONLY the agent number." },
      { role: "user", content: `Agents:\n${menu}\n\nRequest: ${input}` },
    ],
    tools: [],
  });
  const match = pick.text.match(/\d+/);
  const idx = match ? Number.parseInt(match[0], 10) : Number.NaN;
  if (!Number.isInteger(idx) || idx < 0 || idx >= plan.agents.length) {
    throw new AgentError("ROUTER_NO_MATCH", `Router picked invalid agent: ${JSON.stringify(pick.text)}`, false);
  }
  const res = await plan.agents[idx].run(input, { signal: opts.signal, invocationState: opts.invocationState });
  return { strategy: "router", results: [res], text: res.text, routedTo: idx };
}

export async function runOrchestrated(plan: OrchestrationPlan, input: string, signal?: AbortSignal): Promise<OrchestratedResult>;
export async function runOrchestrated(plan: OrchestrationPlan, input: string, opts?: RunOptions): Promise<OrchestratedResult>;
export async function runOrchestrated(
  plan: OrchestrationPlan,
  input: string,
  signalOrOpts?: AbortSignal | RunOptions,
): Promise<OrchestratedResult> {
  const opts: RunOptions = signalOrOpts instanceof AbortSignal ? { signal: signalOrOpts } : (signalOrOpts ?? {});
  switch (plan.strategy) {
    case "sequential":
      return runSequential(plan, input, opts);
    case "parallel":
      return runParallel(plan, input, opts);
    case "router":
      return runRouter(plan, input, opts);
  }
}
