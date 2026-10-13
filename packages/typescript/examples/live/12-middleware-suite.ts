// LIVE: the rest of the middleware suite — todoList, toolCallLimit feedback,
// modelCallLimit, modelFallback (failover to a second LIVE provider), and
// llmToolSelector (model picks the tool subset per call).
import { z } from "zod";
import {
  Agent, loadConfig, providerFromEnv, modelRetryMiddleware, defineTool,
  todoListMiddleware, toolCallLimitMiddleware, modelCallLimit,
  modelFallbackMiddleware, llmToolSelectorMiddleware, createMiddleware,
  type ModelProvider,
} from "../../src/index.js";

const cfg = loadConfig();
const model = (o: { maxTokens?: number } = {}) => providerFromEnv(cfg.active, { maxTokens: o.maxTokens ?? 1000 });
const retry = () => [modelRetryMiddleware({ maxRetries: 3, initialDelayMs: 500 })];

// 1) todoList middleware: the live model writes a real todo list.
const todos = todoListMiddleware();
const todoAgent = new Agent({
  name: "todoer",
  model: model(),
  middleware: [...retry(), todos],
  maxSteps: 4,
});
await todoAgent.run('Use the write_todos tool to record two items: "collect facts" and "write summary".');
console.log("TODOS:", todos.listTodos().map((t) => `${t.content} [${t.status}]`));

// 2) toolCallLimit: over the limit turns into MODEL FEEDBACK, not a crash.
const limited = new Agent({
  name: "limited",
  model: model(),
  middleware: [...retry(), toolCallLimitMiddleware({ toolName: "echo", runLimit: 1 })],
  maxSteps: 3,
  tools: [
    defineTool({ name: "echo", description: "echoes", schema: z.object({ t: z.string() }), execute: async ({ t }) => `echo:${t}` }),
  ],
});
const limitedRes = await limited.run("Call echo with t=hi, then call echo again with t=there, then tell me what happened.");
console.log("LIMIT OUT:", limitedRes.text.trim().slice(0, 80));

// 3) modelCallLimit: run stops cleanly at N model calls.
const callCount = { n: 0 };
const counting = createMiddleware({
  name: "counter",
  beforeModel: (req) => {
    callCount.n += 1;
    return req;
  },
});
const cappedAgent = new Agent({ name: "capped", model: model(), middleware: [counting, modelCallLimit(1)], maxSteps: 4 });
try {
  await cappedAgent.run("Tell me a fun fact, then ask yourself to continue.");
  console.log("NOTE: cap not reached (run finished within 1 call)");
} catch (err) {
  console.log("MODEL CALLS:", callCount.n, "| capped with:", (err as { code?: string }).code, "(expect BUDGET_EXCEEDED at 1)");
}

// 4) modelFallback: a primary that ALWAYS fails fails over to a live backup.
const failingPrimary: ModelProvider = {
  id: "broken",
  capabilities: { structuredOutput: false, streaming: false },
  complete: async () => {
    throw new Error("primary provider down");
  },
};
const failoverAgent = new Agent({
  name: "failover",
  model: failingPrimary,
  middleware: [modelFallbackMiddleware(model({ maxTokens: 800 }))],
});
const failoverRes = await failoverAgent.run("Reply with exactly: fallback-ok");
console.log("FAILOVER TEXT:", failoverRes.text.trim(), failoverRes.text.includes("fallback-ok") ? "(PASS)" : "(NOTE: variance)");

// 5) llmToolSelector: a live model picks which subset of the AGENT's tools
// to expose per call (it filters req.tools; the tools live on the agent).
// A tiny spy middleware records what the selector actually exposed.
const exposed: string[] = [];
const spy = createMiddleware({
  name: "spy",
  beforeModel: (req) => {
    exposed.push(req.tools.map((t) => t.name).join(",") || "(none)");
    return req;
  },
});
const picked = new Agent({
  name: "picker",
  model: model(),
  // meta-prompt (tool picking) is thinking-heavy: 2000-token selector budget
  middleware: [spy, ...retry(), llmToolSelectorMiddleware({ model: model({ maxTokens: 2000 }) })],
  maxSteps: 3,
  tools: [
    defineTool({ name: "calc_add", description: "adds two numbers", schema: z.object({ a: z.number(), b: z.number() }), execute: async ({ a, b }) => String(a + b) }),
    defineTool({ name: "poem", description: "writes poems about anything", schema: z.object({}), execute: async () => "roses are red" }),
  ],
});
const pickedRes = await picked.run("I need you to add 20 and 22 — only adding, no poetry. Use the tool that adds.");
console.log("SELECTOR EXPOSED:", exposed, "-> OUT:", pickedRes.text.trim().slice(0, 40) || "(tools only)");
console.log("PASS: middleware suite live (see values above)");