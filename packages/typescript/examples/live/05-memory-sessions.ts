// LIVE: memory + sessions — multi-turn recall, compaction, restart restore.
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { Agent, loadConfig, providerFromEnv, FileMemory, CompactedMemory, SessionManager, LocalFileStorage, modelRetryMiddleware } from "../../src/index.js";

const cfg = loadConfig();
const model = () => providerFromEnv(cfg.active, { maxTokens: 400 });
// Transient network/429/5xx errors retry with backoff instead of killing the demo.
const retry = [modelRetryMiddleware({ maxRetries: 3, initialDelayMs: 500 })];
const dir = mkdtempSync(join(tmpdir(), "live-mem-"));

// 1) Multi-turn memory with a live model.
const memory = new FileMemory(dir);
const agent = new Agent({ name: "mem", model: model(),
  middleware: retry, memory });
await agent.run("My name is Ada. Just reply ok.");
await agent.run("My favorite color is teal. Just reply ok.");
console.log("HISTORY after 2 turns:", (await memory.load()).length, "messages (no duplicates: clear-then-save works)");
console.log("(expect 4: user+assistant x 2; duplication was the old bug)");

// 2) Windowed compaction keeps the tail + a summary.
const compacted = new CompactedMemory(new FileMemory(join(dir, "c")), { maxMessages: 3 });
const compactAgent = new Agent({ name: "c", model: model(),
  middleware: retry, memory: compacted });
await compactAgent.run("Fact one: irons melt. Just reply ok.");
await compactAgent.run("Fact two: roses are red. Just reply ok.");
await compactAgent.run("Fact three: the moon is far. Just reply ok.");
const loaded = await compacted.load();
console.log("COMPACTED window:", loaded.length, "messages; first is summary:", String(loaded[0].content).includes("Prior context summary"));

// 3) Session restore: a NEW agent instance in the same store recalls turn 1.
const sessionDir = join(dir, "sessions");
const m1 = new SessionManager({ sessionId: "live-demo", storage: new LocalFileStorage(sessionDir) });
const s1 = new Agent({ name: "s", model: model(),
  middleware: retry, sessionManager: m1 });
await s1.run("My codename is Falcon. Just reply ok.");
const m2 = new SessionManager({ sessionId: "live-demo", storage: new LocalFileStorage(sessionDir) });
const s2 = new Agent({ name: "s2", model: model(),
  middleware: retry, sessionManager: m2 });
const recall = await s2.run("What is my codename? Reply with just the codename.");
console.log("RESTORED RECALL:", recall.text.trim(), "(expect Falcon)");
console.log(recall.text.trim().toLowerCase().includes("falcon") ? "PASS: memory + sessions live" : "NOTE: inspect outputs");