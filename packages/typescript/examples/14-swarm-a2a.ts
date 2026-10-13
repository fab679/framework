import { Agent, Swarm, A2AClient, A2AServer } from "../src/index.js";

// Offline demo: swarm handoff + A2A round-trip over localhost.
function scripted(script: Record<number, { to?: string; answer?: string }>) {
  let n = 0;
  return {
    id: "scripted",
    complete: async () => {
      n += 1;
      const s = script[n];
      if (s?.to) {
        return {
          text: "",
          toolCalls: [{ id: `h${n}`, name: "handoff_to_agent", args: { to: s.to, message: "take over", context: { clue: n } } }],
          usage: { inputTokens: 1, outputTokens: 1 },
        };
      }
      return { text: s?.answer ?? "done", toolCalls: [], usage: { inputTokens: 1, outputTokens: 1 } };
    },
  };
}

const swarm = new Swarm({
  agents: [
    new Agent({ name: "researcher", description: "finds facts", model: scripted({ 1: { to: "coder" } }) }),
    new Agent({ name: "coder", description: "writes code", model: scripted({ 1: { answer: "shipped" } }) }),
  ],
});
const out = await swarm.run("ship the todo api");
console.log("SWARM:", out.status, "|", out.text, "| trail:", out.nodeHistory.join(" -> "));
console.log("SHARED:", JSON.stringify(out.sharedContext));

// A2A: serve a Framework agent, consume it back.
const server = new A2AServer({
  card: { name: "echo", description: "echoes back" },
  agentFactory: () => new Agent({ name: "echo", model: scripted({ 1: { answer: "hello-remote" } }) }),
});
const port = await server.start();
try {
  const client = new A2AClient(`http://127.0.0.1:${port}`);
  console.log("CARD:", (await client.getCard()).name);
  console.log("A2A:", (await client.send("hi", "demo-ctx")).text);
} finally {
  await server.stop();
}
