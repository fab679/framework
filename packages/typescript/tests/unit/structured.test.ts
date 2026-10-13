import { describe, expect, it } from "vitest";
import { z } from "zod";
import { Agent } from "../../src/agent/index.js";
import { OpenAICompatibleProvider } from "../../src/models/openai-compatible.js";
import { providerStrategy, toolStrategy } from "../../src/structured/index.js";

const Contact = z.object({ name: z.string(), email: z.string() });

function toolCallingStub(calls: { name: string; args: unknown }[]) {
  let n = 0;
  return {
    id: "stub",
    complete: async () => {
      if (n < calls.length) {
        const c = calls[n++];
        return { text: "", toolCalls: [{ id: `call_${n}`, ...c }], usage: { inputTokens: 1, outputTokens: 1 } };
      }
      return { text: "done", toolCalls: [], usage: { inputTokens: 1, outputTokens: 1 } };
    },
  };
}

describe("structured output — tool strategy", () => {
  it("captures validated output in structuredResponse", async () => {
    const agent = new Agent({
      model: toolCallingStub([{ name: "structured_response", args: { name: "Ada", email: "ada@ex.com" } }]),
      responseFormat: toolStrategy(Contact),
    });
    const res = await agent.run("extract Ada");
    expect(res.structuredResponse).toEqual({ name: "Ada", email: "ada@ex.com" });
  });

  it("feeds validation errors back so the model self-repairs", async () => {
    const agent = new Agent({
      model: toolCallingStub([
        { name: "structured_response", args: { name: "Ada" } }, // missing email
        { name: "structured_response", args: { name: "Ada", email: "ada@ex.com" } },
      ]),
      responseFormat: toolStrategy(Contact),
    });
    const res = await agent.run("extract Ada");
    expect(res.structuredResponse).toEqual({ name: "Ada", email: "ada@ex.com" });
    expect(res.messages.some((m) => m.role === "tool" && String((m.content as string) ?? "").includes("Error"))).toBe(true);
  });

  it("throws STRUCTURED_NO_MATCH when the model never complies", async () => {
    const agent = new Agent({
      model: toolCallingStub([]),
      maxSteps: 1,
      responseFormat: toolStrategy(Contact),
    });
    await expect(agent.run("extract")).rejects.toMatchObject({ code: "STRUCTURED_NO_MATCH" });
  });
});

describe("structured output — provider strategy", () => {
  it("parses native JSON into structuredResponse", async () => {
    let sentBody: Record<string, unknown> = {};
    const provider = new OpenAICompatibleProvider({
      baseUrl: "https://x",
      apiKey: "k",
      model: "m",
      fetchFn: (async (_u: unknown, init: unknown) => {
        sentBody = JSON.parse((init as { body: string }).body) as Record<string, unknown>;
        return new Response(
          JSON.stringify({ choices: [{ message: { content: '{"name":"Ada","email":"ada@ex.com"}' } }], usage: { prompt_tokens: 1, completion_tokens: 1 } }),
          { status: 200 },
        );
      }) as typeof fetch,
    });
    const agent = new Agent({ model: provider, responseFormat: providerStrategy(Contact) });
    const res = await agent.run("extract Ada");
    expect(res.structuredResponse).toEqual({ name: "Ada", email: "ada@ex.com" });
    expect(sentBody).toMatchObject({ response_format: { type: "json_schema", json_schema: { name: "structured_response" } } });
  });

  it("retries invalid JSON within maxSteps", async () => {
    let n = 0;
    const provider = new OpenAICompatibleProvider({
      baseUrl: "https://x",
      apiKey: "k",
      model: "m",
      fetchFn: (async () => {
        n += 1;
        const content = n === 1 ? "not json" : '{"name":"Ada","email":"ada@ex.com"}';
        return new Response(JSON.stringify({ choices: [{ message: { content } }], usage: {} }), { status: 200 });
      }) as typeof fetch,
    });
    const agent = new Agent({ model: provider, maxSteps: 3, responseFormat: providerStrategy(Contact) });
    const res = await agent.run("extract");
    expect(res.structuredResponse).toEqual({ name: "Ada", email: "ada@ex.com" });
    expect(n).toBe(2);
  });

  it("auto picks provider for capable models, tool otherwise", async () => {
    const capable = new OpenAICompatibleProvider({ baseUrl: "https://x", apiKey: "k", model: "m", fetchFn: (async () => new Response(JSON.stringify({ choices: [{ message: { content: '{"a":1}' } }] }), { status: 200 })) as typeof fetch });
    expect(capable.capabilities?.structuredOutput).toBe(true);
    const { MockModelProvider } = await import("../../src/models/index.js");
    expect(new MockModelProvider().capabilities?.structuredOutput).toBe(false);
  });
});
