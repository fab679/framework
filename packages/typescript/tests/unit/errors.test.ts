import { describe, expect, it } from "vitest";
import { z } from "zod";
import { Agent } from "../../src/agent/index.js";
import { AgentError } from "../../src/types/index.js";
import { ToolRegistry, defineTool } from "../../src/tools/index.js";
import { OpenAICompatibleProvider, type CompleteOptions } from "../../src/models/index.js";
import { runWithBudget } from "../../src/runtime/index.js";

/** Stub model: first call requests a tool, second returns text. */
function toolThenText(toolName: string, thinking?: string) {
  let n = 0;
  return {
    id: "stub",
    complete: async (_opts: CompleteOptions) => {
      n += 1;
      if (n === 1) {
        return {
          text: "",
          ...(thinking ? { thinking } : {}),
          toolCalls: [{ id: "call_1", name: toolName, args: {} }],
          usage: { inputTokens: 1, outputTokens: 1 },
        };
      }
      return { text: "done", toolCalls: [], usage: { inputTokens: 1, outputTokens: 1, reasoningTokens: 3 } };
    },
  };
}

describe("structured errors", () => {
  it("UNKNOWN_TOOL from registry", async () => {
    const reg = new ToolRegistry();
    await expect(reg.call("nope", {})).rejects.toMatchObject({ name: "AgentError", code: "UNKNOWN_TOOL", retryable: false });
  });

  it("TOOL_ARG_INVALID from registry", async () => {
    const reg = new ToolRegistry().register(
      defineTool({ name: "t", description: "t", schema: z.object({ n: z.number() }), execute: async () => 1 }),
    );
    await expect(reg.call("t", { n: "x" })).rejects.toMatchObject({ name: "AgentError", code: "TOOL_ARG_INVALID" });
  });

  it("MODEL_ERROR retryable on 500, fatal on 400", async () => {
    const http = (status: number) =>
      new OpenAICompatibleProvider({
        baseUrl: "https://x",
        apiKey: "k",
        model: "m",
        fetchFn: (async () => new Response("boom", { status })) as typeof fetch,
      });
    await expect(http(500).complete({ messages: [], tools: [] })).rejects.toMatchObject({ code: "MODEL_ERROR", retryable: true });
    await expect(http(400).complete({ messages: [], tools: [] })).rejects.toMatchObject({ code: "MODEL_ERROR", retryable: false });
  });

  it("MODEL_ERROR retryable on network failure", async () => {
    const p = new OpenAICompatibleProvider({
      baseUrl: "https://x",
      apiKey: "k",
      model: "m",
      fetchFn: (async () => { throw new TypeError("down"); }) as typeof fetch,
    });
    await expect(p.complete({ messages: [], tools: [] })).rejects.toMatchObject({ code: "MODEL_ERROR", retryable: true });
  });

  it("TIMEOUT from runtime", async () => {
    await expect(runWithBudget(() => new Promise(() => {}), { timeoutMs: 10 })).rejects.toMatchObject({
      code: "TIMEOUT",
      retryable: true,
    });
  });

  it("ABORTED from runtime (no DOMException leak)", async () => {
    const c = new AbortController();
    c.abort();
    await expect(runWithBudget(async () => "x", { signal: c.signal })).rejects.toMatchObject({
      name: "AgentError",
      code: "ABORTED",
    });
  });

  it("BUDGET_EXCEEDED on maxToolCalls", async () => {
    const agent = new Agent({
      model: toolThenText("t"),
      tools: [defineTool({ name: "t", description: "t", schema: z.object({}), execute: async () => 1 })],
      maxToolCalls: 0,
    });
    await expect(agent.run("go")).rejects.toMatchObject({ code: "BUDGET_EXCEEDED" });
  });
});

describe("artifact split", () => {  it("keeps artifact off the model message but on the call record", async () => {
    const agent = new Agent({
      model: toolThenText("search"),
      tools: [
        defineTool({
          name: "search",
          description: "s",
          schema: z.object({}),
          execute: async () => ({ snippet: "seen", docId: "doc_123" }),
          toResult: (out) => ({ content: (out as { snippet: string }).snippet, artifact: { document_id: (out as { docId: string }).docId } }),
        }),
      ],
    });
    const res = await agent.run("go");
    expect(res.toolCalls[0].artifact).toEqual({ document_id: "doc_123" });
    const toolMsg = res.messages.find((m) => m.role === "tool");
    expect(toolMsg?.content).toBe("seen");
  });
});

describe("outputVersion", () => {
  it("v0 drops thinking from history; v1 stores reasoning blocks", async () => {
    const thinker = {
      id: "thinker",
      complete: async () => ({ text: "answer", thinking: "hmm", toolCalls: [], usage: { inputTokens: 1, outputTokens: 1 } }),
    };
    const v0 = await new Agent({ model: thinker, outputVersion: "v0" }).run("q");
    const v1 = await new Agent({ model: thinker, outputVersion: "v1" }).run("q");
    expect(v0.messages.at(-1)?.content).toBe("answer");
    expect(v1.messages.at(-1)?.content).toEqual([
      { type: "reasoning", reasoning: "hmm" },
      { type: "text", text: "answer" },
    ]);
  });
});

describe("tool-call history + execution", () => {
  it("records assistant tool_call messages and accumulates reasoning usage", async () => {
    const agent = new Agent({
      model: toolThenText("t"),
      tools: [defineTool({ name: "t", description: "t", schema: z.object({}), execute: async () => 1 })],
    });
    const res = await agent.run("go");
    const asst = res.messages.find(
      (m) => m.role === "assistant" && typeof m.content !== "string" && m.content.some((b) => b.type === "tool_call"),
    );
    expect(asst).toBeDefined();
    expect(res.toolCalls[0].id).toBe("call_1");
    expect(res.usage.reasoningTokens).toBe(3);
  });

  it("wraps tool bugs in TOOL_EXECUTION, passes AgentError through", async () => {
    const mkAgent = (tool: { name: string }) =>
      new Agent({
        model: toolThenText(tool.name),
        tools: [
          tool.name === "boom"
            ? defineTool({ name: "boom", description: "b", schema: z.object({}), execute: async (): Promise<number> => { throw new Error("bug"); } })
            : defineTool({ name: "coded", description: "c", schema: z.object({}), execute: async (): Promise<number> => { throw new AgentError("CUSTOM", "inner", false); } }),
        ],
      });
    await expect(mkAgent({ name: "boom" }).run("go")).rejects.toMatchObject({ code: "TOOL_EXECUTION" });
    await expect(mkAgent({ name: "coded" }).run("go")).rejects.toMatchObject({ code: "CUSTOM" });
  });

  it("wires assistant tool_calls to the provider payload", async () => {
    let body: Record<string, unknown> = {};
    const p = new OpenAICompatibleProvider({
      baseUrl: "https://x",
      apiKey: "k",
      model: "m",
      fetchFn: (async (_url: unknown, init: unknown) => {
        body = JSON.parse((init as { body: string }).body) as Record<string, unknown>;
        return new Response(JSON.stringify({ choices: [{ message: { content: "ok" } }] }), { status: 200 });
      }) as typeof fetch,
    });
    await p.complete({
      messages: [
        { role: "assistant", content: [{ type: "tool_call", id: "call_1", name: "t", args: { a: 1 } }] },
        { role: "tool", content: "1", toolCallId: "call_1", toolName: "t" },
      ],
      tools: [],
    });
    const msgs = body.messages as Record<string, unknown>[];
    expect(msgs[0]).toMatchObject({ role: "assistant", tool_calls: [{ id: "call_1", type: "function" }] });
    expect(msgs[1]).toMatchObject({ role: "tool", tool_call_id: "call_1" });
  });
});
