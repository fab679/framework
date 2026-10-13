import { AgentError } from "../types/index.js";
import type { InvocationState } from "../state/index.js";
import { sleep } from "../utils/index.js";

/** DAG workflow node. `ctx` maps completed node ids to their results. */
export interface WorkflowNode {
  id: string;
  run: (ctx: Record<string, unknown>, invocationState?: InvocationState) => Promise<unknown>;
  dependsOn?: string[];
  /** Retry policy. Default: no retry (attempts: 1). Abort always rethrows. */
  retry?: { attempts?: number; backoffMs?: number };
}

export interface WorkflowOptions {
  signal?: AbortSignal;
  /** Shared per-run data passed to every node (never sent to models by itself). */
  invocationState?: InvocationState;
}

function sortTopo(nodes: WorkflowNode[]): WorkflowNode[][] {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  if (byId.size !== nodes.length) throw new AgentError("WORKFLOW_DUPLICATE_ID", "Workflow node ids must be unique", false);
  for (const n of nodes) {
    for (const dep of n.dependsOn ?? []) {
      if (!byId.has(dep)) throw new AgentError("WORKFLOW_UNKNOWN_DEP", `Node "${n.id}" depends on unknown node "${dep}"`, false);
      if (dep === n.id) throw new AgentError("WORKFLOW_CYCLE", `Node "${n.id}" depends on itself`, false);
    }
  }
  // Deduplicate: `dependsOn: ["a", "a"]` is one edge, not two — counting it
  // twice wedges indegree at 1 and misreports a legal graph as a cycle.
  const indegree = new Map(nodes.map((n) => [n.id, n.dependsOn ? new Set(n.dependsOn).size : 0]));
  const levels: WorkflowNode[][] = [];
  let ready = nodes.filter((n) => (indegree.get(n.id) ?? 0) === 0);
  while (ready.length) {
    levels.push(ready);
    const next: WorkflowNode[] = [];
    for (const n of nodes) {
      if (!ready.includes(n)) continue;
      for (const m of nodes) {
        if (!(m.dependsOn ?? []).includes(n.id)) continue;
        indegree.set(m.id, (indegree.get(m.id) ?? 1) - 1);
        if (indegree.get(m.id) === 0) next.push(m);
      }
    }
    ready = next;
  }
  if (levels.flat().length !== nodes.length) {
    throw new AgentError("WORKFLOW_CYCLE", "Workflow has a dependency cycle", false);
  }
  return levels;
}

async function runWithRetry(
  node: WorkflowNode,
  ctx: Record<string, unknown>,
  signal?: AbortSignal,
  invocationState?: InvocationState,
): Promise<unknown> {
  const attempts = Math.max(1, node.retry?.attempts ?? 1);
  const backoffMs = node.retry?.backoffMs ?? 0;
  let last: unknown;
  for (let i = 0; i < attempts; i++) {
    signal?.throwIfAborted();
    try {
      return await node.run(ctx, invocationState);
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") throw err;
      last = err;
      if (i < attempts - 1 && backoffMs > 0) await sleep(backoffMs, signal);
    }
  }
  throw last;
}

/**
 * Execute a DAG: topological levels run in order, nodes within a level
 * run concurrently. Returns id -> result. Throws structured WORKFLOW_* errors.
 */
export async function runWorkflow(nodes: WorkflowNode[], opts: WorkflowOptions = {}): Promise<Record<string, unknown>> {
  const ctx: Record<string, unknown> = {};
  for (const level of sortTopo(nodes)) {
    opts.signal?.throwIfAborted();
    const entries = await Promise.all(
      level.map(async (n) => [n.id, await runWithRetry(n, ctx, opts.signal, opts.invocationState)] as const),
    );
    for (const [id, value] of entries) ctx[id] = value;
  }
  return ctx;
}
