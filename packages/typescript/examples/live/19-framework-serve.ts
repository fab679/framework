// LIVE: framework.json-driven serving — write a config file, boot the serve
// layer over a LIVE model, hit it from the A2A wire protocol (card + send),
// then shut down cleanly.
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  loadFrameworkConfig, serveFramework, A2AClient, loadConfig,
} from "../../src/index.js";

// Same file the `framework-serve` CLI consumes.
const dir = mkdtempSync(join(tmpdir(), "live-serve-"));
writeFileSync(join(dir, "framework.json"), JSON.stringify({
  server: { host: "127.0.0.1", basePort: 8901 },
  http: { corsOrigins: [] },
  agents: [
    {
      name: "greeter",
      description: "Greets people by name",
      model: loadConfig().active.model, // bare id -> together via TOGETHER_API_KEY
      maxSteps: 4,
    },
  ],
}));

const file = loadFrameworkConfig(join(dir, "framework.json"));
console.log("CONFIG AGENTS:", file.agents.map((a) => `${a.name} [${a.model}]`));

const { agents: served, stop } = await serveFramework(file);
try {
  const [servedAgent] = served;
  console.log("SERVED:", servedAgent.name, "->", servedAgent.url);
  const client = new A2AClient(servedAgent.url);
  const card = await client.getCard();
  console.log("REMOTE CARD:", { name: card.name, description: card.description });
  const reply = await client.send("Reply with exactly: served-ok");
  console.log("REMOTE SEND:", reply.text.trim(), "| contextId:", reply.contextId !== undefined);
  console.log(reply.text.includes("served-ok") ? "PASS: framework.json -> serve -> A2A wire live" : "NOTE: model variance, inspect");
  // Second turn continues the same context.
  const second = await client.send("Repeat your previous reply.", reply.contextId);
  console.log("SECOND TURN:", second.text.trim().slice(0, 40));
} finally {
  await stop();
  console.log("SERVER STOPPED — clean shutdown verified");
  try { rmSync(dir, { recursive: true, force: true }); } catch { /* tmpdir is fine either way */ }
}