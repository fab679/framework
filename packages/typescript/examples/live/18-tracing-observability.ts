// LIVE: tracing + observability — OTel-shaped spans (JSONL + memory sinks)
// and console events around a real run with a real tool call.
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { z } from "zod";
import type { AgentEvents } from "../../src/index.js";
import {
  Agent, loadConfig, providerFromEnv, consoleEvents, defineTool,
  tracingEvents, JsonlSpanSink, MemorySpanSink, modelRetryMiddleware,
} from "../../src/index.js";

/** AgentEvents is a single hooks object — fan every hook out to all sinks. */
const combine = (...list: AgentEvents[]): AgentEvents => ({
  onStep: (s) => list.forEach((e) => e.onStep?.(s)),
  onToken: (t) => list.forEach((e) => e.onToken?.(t)),
  onThinking: (t) => list.forEach((e) => e.onThinking?.(t)),
  onToolCall: (c) => list.forEach((e) => e.onToolCall?.(c)),
  onToolResult: (r) => list.forEach((e) => e.onToolResult?.(r)),
  onDone: (d) => list.forEach((e) => e.onDone?.(d)),
});

const cfg = loadConfig();
const dir = mkdtempSync(join(tmpdir(), "live-trace-"));
const jsonlPath = join(dir, "trace.jsonl");

const memorySink = new MemorySpanSink();
const spanEvents = tracingEvents(memorySink, { traceId: "live-trace-0001" });
const jsonlSink = new JsonlSpanSink(jsonlPath);
const fileEvents = tracingEvents(jsonlSink, { traceId: "live-trace-0002" });

const agent = new Agent({
  name: "traced",
  model: providerFromEnv(cfg.active, { maxTokens: 800 }),
  middleware: [modelRetryMiddleware({ maxRetries: 3, initialDelayMs: 500 })],
  maxSteps: 4,
  tools: [
    defineTool({
      name: "get_time",
      description: "Returns a fixed time",
      schema: z.object({}),
      execute: async () => "12:00 UTC",
    }),
  ],
  // consoleEvents prints the live stream; spanEvents + fileEvents project it.
  events: combine(consoleEvents("[trace-demo]"), spanEvents, fileEvents),
});

const res = await agent.run("Use the get_time tool, then tell me the time it returned.");
console.log("FINAL:", res.text.trim().slice(0, 60));

const spans = memorySink.spans;
console.log("IN-MEMORY SPANS:", spans.map((s) => s.name), "traceId:", spans[0]?.traceId);
const onDisk = readFileSync(jsonlPath, "utf8").trim().split("\n").map((l) => JSON.parse(l) as { name: string });
console.log("JSONL SPANS ON DISK:", onDisk.map((s) => s.name));
const hasToolSpan = spans.some((s) => s.name.includes("tool"));
console.log(
  hasToolSpan && onDisk.length === spans.length && onDisk.length > 0
    ? "PASS: spans captured live (memory + JSONL match)"
    : "NOTE: inspect span lists above",
);