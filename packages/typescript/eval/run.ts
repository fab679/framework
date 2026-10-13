import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { z } from "zod";
import { Agent, InMemoryMemory, defineTool, loadConfig, providerFromEnv } from "../src/index.js";
import type { EvalCase } from "../src/evals/index.js";
import { loadJsonl, printReport, runEval } from "../src/evals/index.js";
import type { ModelCompletion } from "../src/models/index.js";

/**
 * Seed eval runner: deterministic stubs offline, live provider with EVAL_LIVE=1.
 *   pnpm --filter framework-agents eval
 */

const here = dirname(fileURLToPath(import.meta.url));
const live = process.env.EVAL_LIVE === "1";

function stubFor(c: EvalCase) {
  let n = 0;
  const once = (call: { id: string; name: string; args: unknown }, then: string) => ({
    id: "stub",
    complete: async (): Promise<ModelCompletion> => {
      n += 1;
      if (n === 1) {
        return { text: "", toolCalls: [call], usage: { inputTokens: 1, outputTokens: 1 } };
      }
      return { text: then, toolCalls: [], usage: { inputTokens: 1, outputTokens: 1 } };
    },
  });
  switch (c.id) {
    case "tool-use":
      return once({ id: "c1", name: "add", args: { a: 2, b: 3 } }, "5");
    case "unknown-tool-refusal":
      return once({ id: "c1", name: "teleport", args: {} }, "I cannot do that. Unknown tool: teleport");
    case "multi-step": {
      let n = 0;
      return {
        id: "stub",
        complete: async (): Promise<ModelCompletion> => {
          n += 1;
          if (n === 1) return { text: "", toolCalls: [{ id: "c1", name: "add", args: { a: 1, b: 1 } }], usage: { inputTokens: 1, outputTokens: 1 } };
          if (n === 2) return { text: "", toolCalls: [{ id: "c2", name: "double", args: { n: 2 } }], usage: { inputTokens: 1, outputTokens: 1 } };
          return { text: "4", toolCalls: [], usage: { inputTokens: 1, outputTokens: 1 } };
        },
      };
    }
    default:
      return {
        id: "stub",
        complete: async (): Promise<ModelCompletion> => ({ text: "the code is 4821", toolCalls: [], usage: { inputTokens: 1, outputTokens: 1 } }),
      };
  }
}

const addTool = defineTool({
  name: "add",
  description: "Adds",
  schema: z.object({ a: z.number(), b: z.number() }),
  execute: async ({ a, b }: { a: number; b: number }) => a + b,
});

const doubleTool = defineTool({
  name: "double",
  description: "Doubles",
  schema: z.object({ n: z.number() }),
  execute: async ({ n }: { n: number }) => n * 2,
});

const cases = loadJsonl(join(here, "datasets", "seeds.jsonl"));
const liveProvider = live ? providerFromEnv(loadConfig().active) : undefined;
if (live && !loadConfig().active.apiKey) {
  console.error("EVAL_LIVE=1 needs TOGETHER_API_KEY in .env");
  process.exit(1);
}

const { scores, passRate } = await runEval(
  cases,
  (c) =>
    new Agent({
      model: liveProvider ?? stubFor(c),
      tools: [addTool, doubleTool],
      memory: new InMemoryMemory(),
      maxSteps: 4,
    }),
);
printReport(scores, passRate);
if (passRate < 1) process.exit(1);
