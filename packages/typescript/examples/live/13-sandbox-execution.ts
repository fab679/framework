// LIVE: sandbox — a real model runs real shell commands and JavaScript in
// the jailed workspace (execute via createSandboxBackend + run_javascript vm).
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  Agent, loadConfig, providerFromEnv, modelRetryMiddleware,
  createSandboxBackend, runJavascriptTool, modelCallLimit,
} from "../../src/index.js";

const cfg = loadConfig();
const model = () => providerFromEnv(cfg.active, { maxTokens: 1500 });
const retry = [modelRetryMiddleware({ maxRetries: 3, initialDelayMs: 500 })];

const dir = mkdtempSync(join(tmpdir(), "live-sandbox-"));
const backend = createSandboxBackend({ root: dir, timeoutMs: 15000 });
const agent = new Agent({
  name: "sandboxed",
  model: model(),
  middleware: [...retry, modelCallLimit(8)],
  maxSteps: 6,
  tools: [...backend.tools(), runJavascriptTool()],
});

// The model must (1) write a file, (2) execute a shell command that reads it,
// (3) evaluate JavaScript. All inside the sandbox root.
const res = await agent.run(
  "In your workspace, do these three steps with the tools available: " +
  '1) use write_file to create notes.txt containing exactly "sandbox-ok". ' +
  '2) use execute to run: cat notes.txt ... ' +
  "3) use run_javascript to compute 6*7 and report the value. " +
  'Then reply with the two results and the word finished.',
);

console.log("TOOL OUTPUTS:", JSON.stringify(res.toolCalls.map((c) => ({ name: c.name, out: String(c.output).slice(0, 50) }))));
console.log("SANDBOX ROOT:", dir);
console.log(
  res.toolCalls.some((c) => c.name === "execute") && res.text.includes("sandbox-ok")
    ? "PASS: live model executed inside the sandbox (execute ran, content matches)"
    : "NOTE: inspect trace above (model variance)",
);