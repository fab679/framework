/**
 * Regression tests for full-sweep review findings — each bug here was real
 * and shipped before the fix; keep them pinned.
 */
import { describe, expect, it } from "vitest";
import { z } from "zod";
import { Agent } from "../../src/agent/index.js";
import { InMemoryMemory } from "../../src/memory/index.js";
import { SlidingWindowConversationManager } from "../../src/conversation/index.js";
import { OpenAICompatibleProvider } from "../../src/models/openai-compatible.js";
import { priceFor, usageCost } from "../../src/models/pricing.js";
import { defineTool } from "../../src/tools/index.js";
import type { ModelCompletion } from "../../src/models/index.js";

/** Stub model emitting the given completions in order (last one repeats). */
function scriptSteps(steps: ModelCompletion[]) {
  let n = 0;
  return {
    id: "scripted",
    capabilities: { structuredOutput: false },
    complete: async (): Promise<ModelCompletion> => steps[Math.min(n++, steps.length - 1)],
  };
}

describe("memory: run-end save appends, runLoop must clear first", () => {
  it("does not duplicate history across runs", async () => {
    const model = {
      id: "t",
      complete: async () => ({ text: "ok", toolCalls: [], usage: { inputTokens: 1, outputTokens: 1 } }),
    };
    const memory = new InMemoryMemory();
    const agent = new Agent({ model, memory });
    await agent.run("first");
    await agent.run("second");
    const content = (await memory.load()).map((m) => m.content);
    expect(content).toEqual(["first", "ok", "second", "ok"]);
  });
});

describe("conversationManager.beforeModel same-reference no-op", () => {
  it("sends the full message list when the manager returns the input array", async () => {
    const model = {
      id: "t",
      complete: async (opts: { messages: unknown[] }) => {
        // The old in-place reset emptied the array here (len 1 = system only).
        expect(opts.messages.length).toBeGreaterThan(1);
        return { text: "ok", toolCalls: [], usage: { inputTokens: 1, outputTokens: 1 } };
      },
    };
    // Default SlidingWindow manager delegates to trim() which returns the
    // input reference on non-trim turns.
    const agent = new Agent({ model, conversationManager: new SlidingWindowConversationManager() });
    const result = await agent.run("hello");
    expect(result.text).toBe("ok");
  });
});

describe("responseFormat strategy 'auto'", () => {
  it("resolves auto to provider when the model supports it", async () => {
    const model = {
      id: "t",
      capabilities: { structuredOutput: true },
      complete: async () => ({ text: '{"answer": 42}', toolCalls: [], usage: { inputTokens: 1, outputTokens: 1 } }),
    };
    const agent = new Agent({
      model,
      responseFormat: { schema: z.object({ answer: z.number() }), strategy: "auto" },
    });
    const result = await agent.run("q");
    expect(result.structuredResponse).toEqual({ answer: 42 });
  });

  it("resolves auto to the tool strategy when it doesn't", async () => {
    const model = scriptSteps([
      {
        toolCalls: [{ id: "c1", name: "structured_response", args: { answer: 42 } }],
        text: "",
        usage: { inputTokens: 1, outputTokens: 1 },
      },
      { text: "done", toolCalls: [], usage: { inputTokens: 1, outputTokens: 1 } },
    ]);
    const agent = new Agent({
      model,
      responseFormat: { schema: z.object({ answer: z.number() }), strategy: "auto" },
    });
    const result = await agent.run("q");
    expect(result.structuredResponse).toEqual({ answer: 42 });
  });
});

describe("clone tool dedup (withTools)", () => {
  it("does not re-add middleware-contributed tools on clone", () => {
    const mwTool = defineTool({ name: "mw_tool", description: "x", schema: z.object({}), execute: async () => "y" });
    const extra = defineTool({ name: "extra", description: "x", schema: z.object({}), execute: async () => "y" });
    const model = {
      id: "t",
      complete: async () => ({ text: "ok", toolCalls: [], usage: { inputTokens: 1, outputTokens: 1 } }),
    };
    const middleware = [{ name: "mw", tools: [mwTool] }];
    const agent = new Agent({ model, middleware });
    const clone = agent.withTools(extra);
    const names = clone.tools.map((t) => t.name);
    expect(new Set(names).size).toBe(names.length);
    expect(names).toEqual(["mw_tool", "extra"]);
  });
});

describe("providers receive tool parameter schemas", () => {
  it("sends parameters JSON Schema to OpenAI-compatible endpoints", async () => {
    let sentBody = "";
    const p = new OpenAICompatibleProvider({
      baseUrl: "https://api.together.xyz/v1",
      apiKey: "k",
      model: "m",
      fetchFn: (async (_url: Parameters<typeof fetch>[0], init?: RequestInit) => {
        sentBody = String(init?.body ?? "");
        return new Response(JSON.stringify({ choices: [{ message: { content: "x" } }], usage: { prompt_tokens: 1, completion_tokens: 1 } }), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      }) as typeof fetch,
    });
    await p.complete({
      messages: [{ role: "user", content: "hi" }],
      tools: [{ name: "get_weather", description: "w", parametersSchema: { type: "object", properties: { city: { type: "string" } }, required: ["city"] } }],
    });
    const tools = JSON.parse(sentBody).tools;
    expect(tools[0].function.parameters).toMatchObject({ required: ["city"] });
  });
});

describe("pricing table", () => {
  it("treats ollama: as free even when the name collides with a priced model", () => {
    expect(priceFor("ollama:gpt-4o-mini")).toEqual({ inputPerMillion: 0, outputPerMillion: 0 });
  });

  it("does not double-bill reasoning tokens (subset of outputTokens)", () => {
    const usage = { inputTokens: 1_000_000, outputTokens: 1_000_000, reasoningTokens: 800_000 };
    expect(usageCost(usage, "gpt-4o-mini")).toBeCloseTo(0.75); // 0.15 + 0.60, not 0.15 + 1.08
  });
});

describe("A2A stream contract (client ↔ server)", () => {
  it("delivers client text to the model and surfaces the assigned contextId", async () => {
    const { A2AServer } = await import("../../src/a2a/index.js");
    const { AgentClient } = await import("../../src/client/index.js");
    const seen: string[] = [];
    const model = {
      id: "t",
      complete: async (opts: { messages: { role: string; content: unknown }[] }): Promise<ModelCompletion> => {
        const last = opts.messages.filter((m) => m.role === "user").at(-1);
        seen.push(typeof last?.content === "string" ? last.content : JSON.stringify(last?.content));
        return { text: "ok", toolCalls: [], usage: { inputTokens: 1, outputTokens: 1 } };
      },
    };
    const server = new A2AServer({ card: { name: "s" }, agentFactory: () => new Agent({ name: "s", model }) });
    const port = await server.start();
    try {
      const client = new AgentClient(`http://127.0.0.1:${port}`);
      let context: string | undefined;
      const types: string[] = [];
      // Regression: the client used to POST a body without the JSON-RPC
      // envelope — the server parsed params as undefined and ran every
      // stream on an EMPTY message.
      for await (const e of client.stream("Fix my bug", undefined, undefined, {
        onContext: (id) => (context = id),
      })) {
        types.push(e.type);
      }
      expect(seen.at(-1)).toBe("Fix my bug");
      expect(types.at(-1)).toBe("done");
      // Regression: the contextId came back to the caller (multi-turn continuation).
      expect(context).toBeTruthy();
      // Same contextId → same agent → the second turn is delivered too.
      for await (const e of client.stream("more", context)) void e;
      expect(seen.at(-1)).toBe("more");
    } finally {
      await server.stop();
    }
  });
});