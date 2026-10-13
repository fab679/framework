# Multi-agent

Four coordination layers, mirrored in both packages: orchestration (fixed shapes), workflows (DAGs), swarm (autonomous handoffs), and an eval harness.

## Orchestration

`runOrchestrated(plan, input, opts?)` / `run_orchestrated(plan, input)` with `OrchestrationPlan { strategy, agents, concurrency?, routerModel?, events? }` (`OrchestrationPlan` dataclass in Python):

- **sequential** — a chain: each agent receives the original input plus the previous agent's text (`Previous result (<name>): ...`). Results keep agent order; final text is the last result's text.
- **parallel** — fan-out with a `concurrency` cap (default: all at once); every agent gets the same input; results keep agent order; text is the per-agent texts joined with a blank line. Worker loops share `concurrency` and `stop` variables.
- **router** — one model call (`routerModel`, default the first agent's model) picks an agent by index from a numbered menu (`0: name — description`); **only that agent runs**, feeding its raw result; `routedTo` (`routed_to`) records the index and `text` is the routed agent's answer (`results: [one]`). An unparsable/invalid pick throws `ROUTER_NO_MATCH`.

`OrchestratedResult { strategy, results, text, routedTo? }` / `OrchestratedResult(strategy, results, text, routed_to)`. Plan validation: `ORCHESTRATION_EMPTY` with no agents.

```ts
const result = await runOrchestrated(
  { strategy: "parallel", concurrency: 2, agents: [writer, checker] },
  "Draft a release note for #42",
);
```

## Workflows (DAG)

```ts
import { runWorkflow, WorkflowNode } from "framework-agents";

const graph: WorkflowNode[] = [
  { id: "fetch", run: async () => "raw data" },
  { id: "clean", dependsOn: ["fetch"], run: async (ctx) => ctx["fetch"] },
  { id: "report", dependsOn: ["fetch", "clean"], retry: { attempts: 3, backoffMs: 500 },
    run: async (ctx) => `report from ${ctx["clean"]}` },
];
await runWorkflow(graph); // ctx: id -> result, e.g. { fetch, clean, report }
```

- `run(ctx, invocationState?)` receives a record of **completed node ids → results**; Python's `run_workflow`/`WorkflowNode` are identical in shape (`run(ctx, invocation_state)`).
- Topological **levels** run in order; nodes within a level run concurrently.
- `retry?: { attempts?, backoffMs? }` — default no retry (attempts: 1); aborts always rethrow.
- Structural checks (all non-retryable): duplicate ids → `WORKFLOW_DUPLICATE_ID`; unknown dep → `WORKFLOW_UNKNOWN_DEP`; a node depending on itself or a real cycle → `WORKFLOW_CYCLE`.
- Duplicate entries in `dependsOn` are deduplicated first (`["a", "a"]` is one edge) — counting it twice wedges the indegree and misreports a legal graph as a cycle.
- `WorkflowOptions { signal?, invocationState? }` — shared per-run data passes to every node (never sent to models by itself).

## Swarm

```ts
import { Swarm } from "framework-agents";
const swarm = new Swarm({
  agents: [researcher, writer],
  entryPoint: "researcher",     // Agent instance or name; default agents[0]
  maxHandoffs: 20, maxIterations: 20,       // defaults shown
  loopDetectionWindow: 4,                   // 0 disables detection
});
const result = await swarm.run("Research and draft a Q3 summary");
```

- Every member gets a synthetic `handoff_to_agent` tool cloned onto it (`withTools`), with schema `{ to: string, message: string, context?: object }`. Calling it throws an `AgentError("SWARM_HANDOFF")` whose `.handoff` carries the `HandoffSignal(to, message, context)` (readable via `readHandoffSignal(err)` / `read_handoff_signal`).
- The loop rebuilds each turn's input from shared context: original task, handoff message, agents-so-far history, contributed knowledge (per-agent `context` objects), and the roster (plus the finish-vs-handoff instruction). Non-member handoffs get an error string back, not a crash.
- Safety: `maxHandoffs`/`maxIterations` caps, execution/node timeouts (`executionTimeoutMs`, `nodeTimeoutMs` — combined with the parent signal), **ping-pong detection**: with `loopDetectionWindow > 0`, fewer than `minUniqueAgents` (default `2` when only a window is set — a window alone enables detection) unique agents in the last `window` trail entries fails the run.
- `SwarmResult { status: "COMPLETED"|"FAILED", text, nodeHistory, results, iterations, handoffs, usage, sharedContext, error? }` (`SwarmResult` dataclass in Python: `status`, `text`, `node_history`, `results`, `iterations`, `handoffs`, `usage`, `shared_context`, `error`).

Stream events (`swarm.stream(...)` in both): `node_start {agent}`, `node_done {agent}`, `handoff {from, to}`, `agent_event {agent, event}` (member `StreamEvent`s passthrough), `result {result}` (the final result, also the generator's return value in TS).

## Evals harness

JSONL dataset + scoring + pass-rate reporter:

```ts
import { loadJsonl, runEval, printReport } from "framework-agents";

const cases = loadJsonl("evals/cases.jsonl");   // EvalCase: { id, input, expect? }
const { scores, passRate } = await runEval(cases, (c) => makeAgentFor(c));
printReport(scores, passRate);                  // "PASS/FAIL <id>  <detail>" + pass rate
```

- `EvalExpect`: `contains` (substrings the final text must include), `toolUsed` (tools that must appear in `result.toolCalls`), `noStructuredError` (TS shape only). Errors thrown during the run score as `"threw"` fails rather than crashing the suite.
- Closed-loop uses: score with real tool checks on live data only (TS example: `examples/live/15-evaluation-harness.ts`).

Python equivalents: `load_jsonl`, `run_evals` (returns scores + pass rate), `print_report`, `EvalCase`/`EvalExpect`.

```python
from framework_agents import EvalCase, load_jsonl, print_report, run_evals

cases = load_jsonl("evals/cases.jsonl")
scores, rate = await run_evals(cases, lambda c: make_agent(c))
print_report(scores, rate)
```

## Related

- [Agents & tools](agents-and-tools.md) — what a "member" is in each layer
- [Memory & sessions](memory-sessions.md) — `withTools` clone semantics
- [Live examples](../getting-started/live-examples.md) — orchestration, swarm, workflow, and eval demos in both languages