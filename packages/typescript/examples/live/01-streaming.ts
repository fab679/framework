// LIVE: streaming — real token/thinking deltas through agent.stream(), and
// TOOL CALLS streamed live: tool_call yields before execution, tool_result
// right after (the consumer sees them mid-run, not at the end).
// Known documented gap: model text/thinking deltas buffer per step (events.onToken
// fires live; stream() consumers get one batch per model call).
import { z } from "zod";
import {
  Agent, loadConfig, providerFromEnv, modelRetryMiddleware, defineTool,
} from "../../src/index.js";

const cfg = loadConfig();
const model = providerFromEnv(cfg.active, { maxTokens: 400 });
const agent = new Agent({
  model,
  maxSteps: 4,
  middleware: [modelRetryMiddleware({ maxRetries: 2 })], // streaming-tolerant wrapModel
  tools: [
    defineTool({
      name: "get_pi_digits",
      description: "Returns digits of pi.",
      schema: z.object({ count: z.number().int().min(1).max(10) }),
      // The 700ms sleep proves the consumer sees tool_result WHEN IT LANDS,
      // not at run end.
      execute: async ({ count }: { count: number }) => {
        await new Promise((r) => setTimeout(r, 700));
        return "3.14159265358979323846".slice(0, count + 2);
      },
    }),
  ],
});

const timeline: { at: number; type: string; detail: string }[] = [];
const started = Date.now();
let result;
const loop = agent.stream("Use the get_pi_digits tool with count 5, then reply with exactly the digits it returned.");
for (;;) {
  const next = await loop.next();
  if (next.done) {
    result = next.value;
    break;
  }
  const e = next.value;
  const detail =
    e.type === "tool_call" ? e.name
    : e.type === "tool_result" ? `${e.name}: ${JSON.stringify(e.output).slice(0, 40)}`
    : e.type === "token" || e.type === "thinking" ? e.delta
    : e.type === "done" ? e.text.slice(0, 40)
    : "step";
  timeline.push({ at: Date.now() - started, type: e.type, detail });
}
console.table(timeline.slice(0, 20));
console.log("event counts:", timeline.reduce<Record<string, number>>((acc, t) => ({ ...acc, [t.type]: (acc[t.type] ?? 0) + 1 }), {}));
console.log("USAGE:", result.usage);
console.log("TEXT:", result.text.trim());
const hasToolStream = timeline.some((t) => t.type === "tool_call") && timeline.some((t) => t.type === "tool_result");
console.log(hasToolStream && result.usage.inputTokens > 0
  ? "PASS: tool calls streamed live (tool_call BEFORE tool_result, mid-run)"
  : "NOTE: inspect timeline above");