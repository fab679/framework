import { z } from "zod";
import { Agent, defineTool, type CompleteOptions } from "../src/index.js";

// Offline demo: thinking streams to stdout via onThinking, is stored as
// reasoning blocks (outputVersion v1), and the tool keeps its artifact
// off the model message.
let step = 0;
const stub = {
  id: "stub-thinker",
  complete: async (opts: CompleteOptions) => {
    step += 1;
    if (step === 1) {
      opts.onThinking?.("need the calculator for this…");
      return {
        text: "",
        thinking: "need the calculator for this…",
        toolCalls: [{ id: "call_1", name: "add", args: { a: 2, b: 2 } }],
        usage: { inputTokens: 5, outputTokens: 5 },
      };
    }
    opts.onThinking?.("sum looks right…");
    opts.onToken?.("4");
    return {
      text: "4",
      thinking: "sum looks right…",
      toolCalls: [],
      usage: { inputTokens: 5, outputTokens: 5 },
    };
  },
};

const agent = new Agent({
  model: stub,
  tools: [
    defineTool({
      name: "add",
      description: "Adds two numbers",
      schema: z.object({ a: z.number(), b: z.number() }),
      execute: async ({ a, b }) => ({ sum: a + b, op: `${a}+${b}` }),
      toResult: (out) => ({ content: String(out.sum), artifact: { op: out.op } }),
    }),
  ],
  outputVersion: "v1",
  maxSteps: 2,
  events: {
    onThinking: (t) => process.stdout.write(`\x1b[2m[thinking] ${t}\x1b[0m\n`),
    onToken: (t) => process.stdout.write(`[token] ${t}\n`),
  },
});

const res = await agent.run("What is 2 + 2?");
console.log("FINAL:", res.text);
console.log("HISTORY:", JSON.stringify(res.messages, null, 1).slice(0, 600));
console.log("ARTIFACT:", res.toolCalls[0]?.artifact);
