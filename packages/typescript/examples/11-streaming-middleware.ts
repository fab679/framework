import { z } from "zod";
import { Agent, collectStream, defineTool, modelCallLimit, toolRetry, type CompleteOptions } from "../src/index.js";

// Offline demo: typed event stream + middleware (call cap + flaky-tool retry).
let step = 0;
const stub = {
  id: "stub",
  complete: async (opts: CompleteOptions) => {
    step += 1;
    if (step === 1) {
      opts.onThinking?.("should compute this…");
      return { text: "", thinking: "should compute this…", toolCalls: [{ id: "c1", name: "add", args: { a: 20, b: 22 } }], usage: { inputTokens: 1, outputTokens: 1 } };
    }
    opts.onToken?.("42");
    return { text: "42", toolCalls: [], usage: { inputTokens: 1, outputTokens: 1 } };
  },
};

let attempts = 0;
const agent = new Agent({
  model: stub,
  tools: [
    defineTool({
      name: "add",
      description: "Adds",
      schema: z.object({ a: z.number(), b: z.number() }),
      execute: async ({ a, b }) => { attempts += 1; if (attempts < 2) throw new Error("flaky"); return a + b; },
    }),
  ],
  middleware: [modelCallLimit(5), toolRetry({ attempts: 3 })],
});

const { events, result } = await collectStream(agent.stream("20 + 22?"));
console.log("EVENTS:", events.map((e) => e.type).join(" -> "));
console.log("FINAL:", result.text);
