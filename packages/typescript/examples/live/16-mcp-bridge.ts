// LIVE: MCP — a real stdio MCP server child process, bridged into framework
// tools, driven by a live model call. The server is a self-contained Node
// script this example writes and spawns (the same JSON-RPC protocol real
// MCP servers speak over stdio).
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import {
  Agent, loadConfig, providerFromEnv, modelRetryMiddleware,
  connectMcpStdio, mcpToolsToFramework,
} from "../../src/index.js";

const cfg = loadConfig();

// Minimal stdio MCP server: initialize -> notifications/initialized ->
// tools/list -> tools/call, one tool: word_count.
const serverPath = join(mkdtempSync(join(tmpdir(), "live-mcp-")), "server.mjs");
writeFileSync(
  serverPath,
  [
    "import { createInterface } from 'node:readline';",
    "const respond = (id, result) => process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id, result }) + '\\n');",
    "const rl = createInterface({ input: process.stdin });",
    "rl.on('line', (line) => {",
    "  const t = line.trim();",
    "  if (!t) return;",
    "  let msg; try { msg = JSON.parse(t); } catch { return; }",
    "  if (msg.id === undefined) return; // notifications get no response",
    "  if (msg.method === 'initialize') return respond(msg.id, { serverInfo: { name: 'demo-mcp', version: '0' }, capabilities: {} });",
    "  if (msg.method === 'tools/list') return respond(msg.id, { tools: [{ name: 'word_count', description: 'Counts words in a text', inputSchema: { type: 'object', properties: { text: { type: 'string' } }, required: ['text'] } }] });",
    "  if (msg.method === 'tools/call') {",
    "    const n = String(msg.params?.arguments?.text ?? '').trim().split(/\\s+/).filter(Boolean).length;",
    "    return respond(msg.id, { content: [{ type: 'text', text: String(n) }], isError: false });",
    "  }",
    "  respond(msg.id, { error: { code: -32601, message: 'unknown ' + msg.method } });",
    "});",
  ].join("\n"),
);

const client = await connectMcpStdio(process.execPath, [serverPath]);
try {
  const tools = await mcpToolsToFramework(client);
  console.log("BRIDGED TOOLS:", tools.map((t) => t.name));

  const agent = new Agent({
    name: "mcp-user",
    model: providerFromEnv(cfg.active, { maxTokens: 900 }),
    middleware: [modelRetryMiddleware({ maxRetries: 3, initialDelayMs: 500 })],
    maxSteps: 4,
    tools,
  });
  const res = await agent.run('Use the word_count tool with the text "one two three four five" and reply with the number it returns.');
  console.log("MCP TOOL RESULT:", res.toolCalls.find((c) => c.name === "word_count")?.output);
  console.log("AGENT:", res.text.trim().slice(0, 60));
  console.log(res.text.includes("5") ? "PASS: live model -> bridged MCP stdio tool" : "NOTE: inspect trace (model variance)");
} finally {
  await client.close();
}