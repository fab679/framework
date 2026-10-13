// LIVE: swarm — autonomous handoffs between live agents, typed stream events.
import { Agent, loadConfig, providerFromEnv, Swarm, modelRetryMiddleware } from "../../src/index.js";

const cfg = loadConfig();
const model = () => providerFromEnv(cfg.active, { maxTokens: 300 });
// Transient network/429/5xx errors retry with backoff instead of killing the demo.
const retry = [modelRetryMiddleware({ maxRetries: 3, initialDelayMs: 500 })];

const researcher = new Agent({
  name: "researcher",
  description: "Gathers the key facts needed for the task",
  systemPrompt:
    "You produce ONE short key fact list, then call handoff_to_agent to the writer with a handoff message containing those facts. Never answer the user directly.",
  model: model(),
  middleware: retry,
});
const writer = new Agent({
  name: "writer",
  description: "Writes the final answer from the facts",
  systemPrompt: "You write the final answer in one sentence from the handoff message. Do not call handoff_to_agent; complete the task.",
  model: model(),
  middleware: retry,
});

const swarm = new Swarm({
  agents: [researcher, writer],
  entryPoint: "researcher",
  maxHandoffs: 2,
  maxIterations: 3,
  loopDetectionWindow: 4, // default minUniqueAgents kicks in (2)
});

const events: string[] = [];
let result;
const loop = swarm.stream("State the boiling point of water in one sentence.", { sharedState: { note: "e2e" } });
for (;;) {
  const next = await loop.next();
  if (next.done) {
    result = next.value;
    break;
  }
  events.push(next.value.type === "handoff" ? `handoff->${next.value.to}` : next.value.type);
}
console.log("SWARM EVENTS:", events);
console.log("STATUS:", result.status, "| history:", result.nodeHistory, "| handoffs:", result.handoffs);
console.log("FINAL TEXT:", result.text);
console.log("SWARM USAGE (cumulative across members):", result.usage);
console.log(
  events.includes("handoff->writer") && result.status === "COMPLETED"
    ? "PASS: live handoff reached the writer and completed"
    : "NOTE: inspect trace above (models vary)",
);