import { describe, expect, it } from "vitest";
import { z } from "zod";
import { Agent } from "../../src/agent/index.js";
import { loadConfig } from "../../src/config/index.js";
import { providerFromEnv } from "../../src/models/openai-compatible.js";
import { priceFor, usageCost } from "../../src/models/pricing.js";
import { defineTool } from "../../src/tools/index.js";
import { modelRetryMiddleware } from "../../src/middleware/index.js";
import { toolStrategy } from "../../src/structured/index.js";
import { A2AServer } from "../../src/a2a/index.js";
import { AgentClient } from "../../src/client/index.js";

// Live test — TogetherAI only. Network calls, real billing (cents per run).
//   E2E_MODEL=1 pnpm --filter framework-agents test -- --run tests/e2e/together.live.test.ts
const LIVE = process.env.E2E_MODEL === "1";

function liveModel(opts: { stream?: boolean } = {}) {
  const cfg = loadConfig();
  expect(cfg.active.apiKey, "set TOGETHER_API_KEY in framework/.env").not.toBe("");
  expect(cfg.active.source).toBe("together");
  return providerFromEnv(cfg.active, { ...opts, maxTokens: 512 });
}

describe.skipIf(!LIVE)("togetherai live — real payloads", () => {
  it("reports usage + costUsd from a real invocation", async () => {
    const agent = new Agent({ model: liveModel(), middleware: [modelRetryMiddleware({ maxRetries: 2, initialDelayMs: 500 })] });
    const res = await agent.run("Reply with exactly: ok");
    expect(res.text.length).toBeGreaterThan(0);
    // Real usage (the old bug: streaming zeros; verify non-stream too).
    expect(res.usage.inputTokens).toBeGreaterThan(0);
    expect(res.usage.outputTokens).toBeGreaterThan(0);
    const price = priceFor(`openai-compatible:${loadConfig().active.model}`);
    expect(price).toBeDefined();
    // costUsd must be the table price applied to the real counts.
    const expected = price && usageCost(res.usage, `openai-compatible:${loadConfig().active.model}`);
    expect(res.usage.costUsd).toBeDefined();
    expect(Math.abs((res.usage.costUsd ?? 0) - (expected ?? 0))).toBeLessThan(1e-9);
  }, 120_000);

  it("streams thinking via delta.reasoning (Together field) and keeps subset invariant", async () => {
    const thoughts: string[] = [];
    const tokens: string[] = [];
    let used: { inputTokens: number; outputTokens: number; reasoningTokens?: number } | undefined;
    const agent = new Agent({
      model: liveModel({ stream: true }),
      middleware: [modelRetryMiddleware({ maxRetries: 2, initialDelayMs: 500 })],
    });
    const stream = agent.stream("Think briefly, then answer: what is 6 times 7? Reply with only the number.");
    for (;;) {
      const next = await stream.next();
      if (next.done) {
        used = next.value.usage;
        break;
      }
      if (next.value.type === "thinking") thoughts.push(next.value.delta);
      if (next.value.type === "token") tokens.push(next.value.delta);
    }
    // Streaming-mode usage is real (regression: hardcoded {0,0}).
    expect(used?.inputTokens ?? 0).toBeGreaterThan(0);
    expect(used?.outputTokens ?? 0).toBeGreaterThan(0);
    if (used && used.reasoningTokens != null) {
      // Together bills reasoning inside completion_tokens — subset invariant.
      expect(used.reasoningTokens).toBeLessThanOrEqual(used.outputTokens);
    }
    expect(tokens.join("")).toContain("42");
  }, 180_000);

  it("sends tool parameter schemas — the model calls with real arguments", async () => {
    const seenArgs: { city?: string }[] = [];
    const weather = defineTool({
      name: "get_weather",
      description: "Get the current weather for a city.",
      schema: z.object({ city: z.string().min(1) }),
      execute: async ({ city }: { city: string }) => {
        seenArgs.push({ city });
        return { city, celsius: 21, note: "fixture value for live test" };
      },
    });
    const agent = new Agent({ model: liveModel(), tools: [weather], maxSteps: 4, middleware: [modelRetryMiddleware({ maxRetries: 2, initialDelayMs: 500 })] });
    const res = await agent.run("Call the get_weather tool for Tokyo. Then state the temperature you got.");
    // THE regression: without parameters on the wire the model cannot pass a city.
    expect(seenArgs.length).toBeGreaterThan(0);
    const city = seenArgs[0]?.city ?? "";
    expect(typeof city).toBe("string");
    expect(city.length).toBeGreaterThan(0);
    expect(res.toolCalls.some((c) => c.name === "get_weather")).toBe(true);
  }, 180_000);

  it("structured output: provider (auto) strategy gets a valid typed answer", async () => {
    const agent = new Agent({
      model: liveModel(),
      responseFormat: { schema: z.object({ answer: z.number() }), strategy: "auto" },
      maxSteps: 4,
      middleware: [modelRetryMiddleware({ maxRetries: 2, initialDelayMs: 500 })],
    });
    const res = await agent.run("What is 6 times 7? Answer as {\"answer\": <number>}.");
    expect(res.structuredResponse).toEqual({ answer: 42 });
  }, 180_000);

  it("structured output: tool strategy self-repairs on a live model", async () => {
    const agent = new Agent({
      model: liveModel(),
      responseFormat: toolStrategy(z.object({ answer: z.number() })),
      maxSteps: 4,
      middleware: [modelRetryMiddleware({ maxRetries: 2, initialDelayMs: 500 })],
    });
    const res = await agent.run("What is 6 times 7? Use the structured_response tool.");
    expect(res.structuredResponse).toEqual({ answer: 42 });
  }, 180_000);

  it("budgets fire on real usage (maxTokens: 1 must exceed)", async () => {
    const agent = new Agent({ model: liveModel(), budgets: { maxTokens: 1 }, middleware: [modelRetryMiddleware({ maxRetries: 2, initialDelayMs: 500 })] });
    await expect(agent.run("Reply with anything")).rejects.toMatchObject({ code: "BUDGET_EXCEEDED" });
  }, 120_000);

  it("A2A contract over the wire: envelope, contextId chain, multi-turn memory", async () => {
    const agentFactory = () => new Agent({ name: "live", model: liveModel(), middleware: [modelRetryMiddleware({ maxRetries: 2, initialDelayMs: 500 })] });
    const server = new A2AServer({
      card: { name: "live", description: "e2e" },
      agentFactory,
      maxContexts: 10,
    });
    const port = await server.start();
    try {
      const client = new AgentClient(`http://127.0.0.1:${port}`);
      let contextId: string | undefined;
      const events1: string[] = [];
      for await (const e of client.stream("My favorite number is 91. Just remember it and reply ok.", undefined, undefined, {
        onContext: (id) => (contextId = id),
      })) {
        events1.push(e.type);
      }
      // Regression 1: the client used to POST without the JSON-RPC envelope —
      // the server ran every stream on an EMPTY message. A run that says "ok"
      // only proves delivery if the NEXT turn can recall the number.
      expect(events1).toContain("done");
      expect(contextId).toBeTruthy();
      const events2: string[] = [];
      let text2 = "";
      for await (const e of client.stream("What is my favorite number? Reply with just the number.", contextId)) {
        events2.push(e.type);
        if (e.type === "done") text2 = e.text;
      }
      expect(text2).toMatch(/91/);
    } finally {
      await server.stop();
    }
  }, 240_000);
});