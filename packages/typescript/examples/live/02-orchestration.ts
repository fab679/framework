// LIVE: micro-orchestration — sequential chain, parallel, model-picked router.
import { Agent, loadConfig, providerFromEnv, runOrchestrated, modelRetryMiddleware } from "../../src/index.js";

const cfg = loadConfig();
// Thinking models burn tokens resolving chained context (the sequential
// hand-off is deliberately ambiguous for the demo: two numbers in one
// message). 700 burned ENTIRELY on thinking trips the provider's
// finish_reason=length guard loudly instead of a silent empty answer.
const model = () => providerFromEnv(cfg.active, { maxTokens: 4000 });
// Transient network/429/5xx errors retry with backoff instead of killing the demo.
const retry = [modelRetryMiddleware({ maxRetries: 3, initialDelayMs: 500 })];

// 1) Sequential: output of one agent feeds the next.
const seqA = new Agent({ name: "math", description: "arithmetic", model: model(),
  middleware: retry, systemPrompt: "Answer with just the number." });
const seqB = new Agent({ name: "halver", description: "halves numbers", model: model(),
  middleware: retry, systemPrompt: "Reply with just half of the number you are given." });
const seq = await runOrchestrated({ strategy: "sequential", agents: [seqA, seqB] }, "What is 100 + 24?");
console.log("SEQUENTIAL 100+24 then halved:", seq.text, "(expect 62)");

// 2) Parallel with a concurrency cap.
const par = await runOrchestrated(
  {
    strategy: "parallel",
    concurrency: 2,
    agents: [
      new Agent({ name: "a", model: model(),
  middleware: retry, systemPrompt: "Reply with exactly: AAA" }),
      new Agent({ name: "b", model: model(),
  middleware: retry, systemPrompt: "Reply with exactly: BBB" }),
      new Agent({ name: "c", model: model(),
  middleware: retry, systemPrompt: "Reply with exactly: CCC" }),
    ],
  },
  "Respond as instructed.",
);
console.log("PARALLEL results:", par.results.map((r) => r.text.trim()));
console.log("PARALLEL combined:", par.text.length > 0 ? "joined ok" : "EMPTY");

// 3) Router: the agent DESCRIPTIONS feed the menu; the model picks.
const router = await runOrchestrated(
  {
    strategy: "router",
    agents: [
      new Agent({ name: "poet", description: "writes poetry", model: model(),
  middleware: retry, systemPrompt: "Reply with a one-line poem." }),
      new Agent({ name: "calculator", description: "does arithmetic", model: model(),
  middleware: retry, systemPrompt: "Answer with just the number." }),
    ],
  },
  "What is 12 times 12?",
);
console.log("ROUTER picked agent index:", router.routedTo, `(expect 1 = calculator); results[0] is the routed run:`);
console.log("ROUTER answer:", router.results[0]?.text.trim(), "(expect 144)");

const pass = seq.text.trim().includes("62")
  && router.routedTo === 1
  && router.results[0]?.text.trim().includes("144");
console.log(pass ? "PASS: orchestration live" : "NOTE: model variance — inspect outputs above");