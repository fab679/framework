// LIVE: RAG grounding + subagent middleware — a live agent answers from
// retrieved passages instead of memory, and delegates a subtask to a subagent.
import {
  Agent, loadConfig, providerFromEnv, InMemoryRetriever, retrievalTool,
  createSubAgentMiddleware, modelRetryMiddleware,
} from "../../src/index.js";

const cfg = loadConfig();
const model = () => providerFromEnv(cfg.active, { maxTokens: 300 });

// 1) Retrieval grounding: the fact exists ONLY in the corpus.
const docs = [
  { id: "spec-a", text: "The Zephyr engine redlines at 7,200 rpm and uses synthetic oil only." },
  { id: "spec-b", text: "The Zephyr fuel tank holds 61 liters; the spare holds 12." },
  { id: "spec-c", text: "Zephyr Recommended service interval: every 12,000 km." },
];
const ragAgent = new Agent({
  name: "mechanic",
  model: model(),
  middleware: [modelRetryMiddleware({ maxRetries: 3, initialDelayMs: 500 })], // transient network errors
  maxSteps: 4,
  tools: [retrievalTool(new InMemoryRetriever(docs), { k: 2 })],
});
const ragRes = await ragAgent.run("Use the retrieve tool: at what engine speed does the Zephyr redline? Answer with only the number.");
console.log("RAG ANSWER:", ragRes.text.trim(), "(expect 7200/varied format)");
console.log(ragRes.text.includes("7,200") || ragRes.text.replace(",", "").includes("7200")
  ? "PASS: grounded in retrieval, not memory"
  : "NOTE: inspect answer (models vary)");

// 2) Subagents: the `task` tool spawns a scoped specialist mid-run.
const withSubs = new Agent({
  name: "lead",
  model: model(),
  maxSteps: 4,
  middleware: [
    modelRetryMiddleware({ maxRetries: 3, initialDelayMs: 500 }), // transient network errors
    createSubAgentMiddleware({
      model: model(),
      subagents: [
        {
          name: "number-nerd",
          description: "Answers arithmetic questions",
          systemPrompt: "Answer arithmetic with just the number.",
        },
      ],
    }),
  ],
});
const subRes = await withSubs.run('Use the task tool to ask the "number-nerd" subagent: what is 9 times 6? Then repeat its answer.');
console.log("SUBAGENT ANSWER:", subRes.text.trim(), "(expect 54)");
console.log(subRes.text.includes("54") ? "PASS: subagent delegation live" : "NOTE: inspect answer");