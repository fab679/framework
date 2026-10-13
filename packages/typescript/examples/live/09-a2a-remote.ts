// LIVE: A2A over the wire — a local A2A server hosts a live agent; a client
// streams it for two turns; and a2aAsTool lets ANOTHER agent call it as a tool.
import { Agent, loadConfig, providerFromEnv, A2AServer, A2AClient, AgentClient, a2aAsTool, modelRetryMiddleware } from "../../src/index.js";

const cfg = loadConfig();

const server = new A2AServer({
  card: { name: "specialist", description: "Knows capitals" },
  agentFactory: () =>
    new Agent({
      name: "specialist",
      description: "Knows capitals",
      systemPrompt: "Answer capital-city questions in one word.",
      model: providerFromEnv(cfg.active, { maxTokens: 300 }),
      middleware: [modelRetryMiddleware({ maxRetries: 3, initialDelayMs: 500 })], // transient network errors
    }),
});
const port = await server.start();
const url = `http://127.0.0.1:${port}`;
console.log("A2A server up:", url, "(card:", `${url}/.well-known/agent-card.json)`);

try {
  // 1) Card discovery + two streaming turns through one context.
  // (AgentClient carries the SSE /stream contract; A2AClient is JSON-RPC send/card.)
  const client = new AgentClient(url);
  console.log("CARD:", await client.card());

  let contextId: string | undefined;
  const turn = async (message: string) => {
    let text = "";
    const loop = client.stream(message, contextId, AbortSignal.timeout(90_000), {
      onContext: (id: string) => (contextId = id),
    });
    for (;;) {
      const next = await loop.next();
      if (next.done) break;
      if (next.value.type === "done") text = next.value.text;
    }
    return text;
  };

  const t1 = await turn("The capital of France? One word.");
  const t2 = await turn("And of Japan? One word.");
  console.log("TURN 1:", t1.trim(), "| TURN 2:", t2.trim(), "| shared contextId:", contextId !== undefined);

  // 2) a2aAsTool: a live orchestrating agent calls the remote agent as a tool.
  const remote = a2aAsTool(new A2AClient(url), { name: "ask_specialist", description: "Ask the remote capitals specialist." });
  const orchestrator = new Agent({
    name: "orchestrator",
    model: providerFromEnv(cfg.active, { maxTokens: 300 }),
    middleware: [modelRetryMiddleware({ maxRetries: 3, initialDelayMs: 500 })], // transient network errors
    maxSteps: 4,
    tools: [remote],
  });
  const res = await orchestrator.run("Use the ask_specialist tool to ask for the capital of Italy, then repeat it verbatim.");
  console.log("A2A-AS-TOOL:", res.text.trim());
  console.log(
    t1.toLowerCase().includes("paris") && res.text.length > 0 && contextId !== undefined
      ? "PASS: A2A live (stream x2 + remote-as-tool)"
      : "NOTE: inspect outputs (models vary)",
  );
} finally {
  await server.stop();
}