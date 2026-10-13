// LIVE: the eval harness — a real JSONL dataset scored against live model
// runs (contains + tool-used assertions, pass-rate reporter).
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { z } from "zod";
import {
  Agent, loadConfig, providerFromEnv, modelRetryMiddleware, defineTool,
  loadJsonl, runEval, printReport,
} from "../../src/index.js";

const cfg = loadConfig();
const retry = [modelRetryMiddleware({ maxRetries: 3, initialDelayMs: 500 })];

const makeAgent = () =>
  new Agent({
    name: "eval-agent",
    model: providerFromEnv(cfg.active, { maxTokens: 900 }),
    middleware: retry,
    maxSteps: 4,
    tools: [
      defineTool({
        name: "calculator",
        description: "Evaluates arithmetic",
        schema: z.object({ expression: z.string() }),
        execute: async (_args: { expression: string }) => "42",
      }),
    ],
  });

// A tiny JSONL dataset on disk — the exact format eval/run.ts consumes.
const dataset = [
  { id: "math-1", input: "Use the calculator tool to compute the answer to life, then reply with the number it returned.", expect: { contains: ["42"], toolUsed: ["calculator"] } },
  { id: "facts-1", input: "Reply with exactly: the capital of France is Paris", expect: { contains: ["Paris"] } },
  { id: "facts-2", input: "Reply with exactly three words: water freezes at zero", expect: { contains: ["zero"] } },
];
const path = join(mkdtempSync(join(tmpdir(), "live-eval-")), "cases.jsonl");
writeFileSync(path, dataset.map((c) => JSON.stringify(c)).join("\n"));

const cases = loadJsonl(path);
console.log(`loaded ${cases.length} cases from ${path}`);
const { scores, passRate } = await runEval(cases, makeAgent);
printReport(scores, passRate);
console.log(passRate === 1 ? "PASS: eval harness live, all cases green" : `NOTE: ${(1 - passRate) * 100}% failed — inspect failures above`);