import { Agent, runOrchestrated, runWorkflow } from "../src/index.js";

// Offline demo: a two-agent chain, a parallel fan-out, a router,
// and a small DAG workflow — all on stub models, no network.
function echoAgent(name: string, description = "") {
  return new Agent({
    name,
    description,
    model: {
      id: `echo:${name}`,
      complete: async (opts) => ({
        text: `${name} saw: ${opts.messages.at(-1)?.content}`,
        toolCalls: [],
        usage: { inputTokens: 1, outputTokens: 1 },
      }),
    },
  });
}

const sequential = await runOrchestrated(
  { strategy: "sequential", agents: [echoAgent("researcher"), echoAgent("writer")] },
  "draft a haiku about rivers",
);
console.log("SEQUENTIAL:", sequential.text.slice(0, 120));

const parallel = await runOrchestrated(
  { strategy: "parallel", agents: [echoAgent("a"), echoAgent("b"), echoAgent("c")], concurrency: 2 },
  "vote: cats or dogs?",
);
console.log("PARALLEL:", parallel.text.split("\n\n").length, "results");

const router = await runOrchestrated(
  {
    strategy: "router",
    agents: [echoAgent("billing", "invoices"), echoAgent("shipping", "parcels")],
    routerModel: {
      id: "picker",
      complete: async () => ({ text: "1", toolCalls: [], usage: { inputTokens: 1, outputTokens: 1 } }),
    },
  },
  "where is my parcel?",
);
console.log("ROUTER ->", router.routedTo, ":", router.text.slice(0, 80));

const ctx = await runWorkflow([
  { id: "fetch", run: async () => [1, 2, 3] },
  { id: "double", dependsOn: ["fetch"], run: async (c) => (c.fetch as number[]).map((n) => n * 2) },
  { id: "sum", dependsOn: ["double"], run: async (c) => (c.double as number[]).reduce((a, b) => a + b, 0) },
]);
console.log("WORKFLOW sum =", ctx.sum);
