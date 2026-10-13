/**
 * LIVE example — real TogetherAI calls (needs TOGETHER_API_KEY in .env).
 * Exercises the fixes the live e2e suite pins: streaming usage accounting,
 * thinking via delta.reasoning, tool parameter schemas, costUsd reporting.
 * Run: pnpm --filter framework-agents dev -- examples/07-together-live.ts
 */
import { z } from "zod";
import { Agent, loadConfig, providerFromEnv, consoleEvents, defineTool } from "../src/index.js";

const cfg = loadConfig();
console.log(`provider: ${cfg.active.source} model=${cfg.active.model}`);
if (!cfg.active.apiKey) {
  console.error("Missing API key. Put TOGETHER_API_KEY=... in framework/.env then retry.");
  process.exit(1);
}

// 1) Plain run: real usage + cost.
const agent = new Agent({
  model: providerFromEnv(cfg.active, { maxTokens: 512 }),
  events: consoleEvents(),
});
const res = await agent.run("Reply with exactly: ok");
console.log("FINAL:", res.text);
console.log("USAGE:", res.usage, "(costUsd is table price x real token counts)");

// 2) Streaming with thinking: deltas flow live, usage is captured from the
//    final chunk (stream_options.include_usage), reasoning billed as output.
const thoughts: string[] = [];
const streamed = new Agent({
  model: providerFromEnv(cfg.active, { stream: true, maxTokens: 512 }),
});
const streamLoop = streamed.stream("Think briefly, then tell me the capital of France in one word.");
let streamResult: Awaited<ReturnType<typeof streamed.run>> | undefined;
for (;;) {
  const next = await streamLoop.next();
  if (next.done) {
    streamResult = next.value;
    break;
  }
  if (next.value.type === "thinking") thoughts.push(next.value.delta);
}
console.log("thinking streamed:", thoughts.length, "deltas");
console.log("STREAM USAGE:", streamResult?.usage);

// 3) Tool call with a REAL argument schema — the model must name the city.
const seen: string[] = [];
const withTools = new Agent({
  model: providerFromEnv(cfg.active, { maxTokens: 512 }),
  maxSteps: 4,
  tools: [
    defineTool({
      name: "get_weather",
      description: "Get the current weather for a city.",
      schema: z.object({ city: z.string() }),
      execute: async ({ city }: { city: string }) => {
        seen.push(city);
        return { city, celsius: 18 };
      },
    }),
  ],
});
await withTools.run("Call get_weather for Paris, then report the temperature.");
console.log("TOOL CALLED WITH CITY:", JSON.stringify(seen));

// 4) Structured output through the auto strategy (provider on Together).
const structuredAgent = new Agent({
  model: providerFromEnv(cfg.active, { maxTokens: 512 }),
  maxSteps: 4,
  responseFormat: { schema: z.object({ answer: z.number(), unit: z.literal("celsius") }), strategy: "auto" },
});
const structured = await structuredAgent.run("How many days are in a week? Answer with {\"answer\": <number>, \"unit\": \"celsius\"}.");
console.log("STRUCTURED:", structured.structuredResponse);