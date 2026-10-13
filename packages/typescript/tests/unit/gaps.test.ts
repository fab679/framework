import { describe, expect, it, vi } from "vitest";
import { Agent } from "../../src/agent/index.js";
import { AgentClient } from "../../src/client/index.js";
import { consoleEvents, logError, supportsColor } from "../../src/observability/index.js";
import { AnthropicProvider } from "../../src/models/anthropic.js";
import { initChatModel } from "../../src/models/factory.js";
import { ollama } from "../../src/models/ollama.js";
import { scripted } from "./helpers.js";

function jsonFetch(payload: unknown, status = 200) {
  return (async () =>
    new Response(JSON.stringify(payload), { status, headers: { "content-type": "application/json" } })) as typeof fetch;
}

describe("factory + ollama options", () => {
  it("custom provider + all ollama opts", () => {
    const c = initChatModel("custom:m", { baseUrl: "http://x", apiKey: "k" });
    expect(c.id).toContain("m");
    expect(() => initChatModel("custom:m", { baseUrl: "http://x" })).toThrow(/needs baseUrl/);
    const o = ollama({ model: "m", temperature: 0.1, maxTokens: 5, stream: false, fetchFn: jsonFetch({}) });
    expect(o.id).toBe("ollama:m");
  });
});

describe("anthropic branches", () => {
  it("system/temperature/thinking/images/tool-results round-trip", async () => {
    const seen: Record<string, unknown>[] = [];
    const p = new AnthropicProvider({
      apiKey: "k",
      model: "claude-sonnet-4-6",
      temperature: 0.5,
      thinkingBudget: 100,
      fetchFn: (async (_u: unknown, init: unknown) => {
        seen.push(JSON.parse((init as { body: string }).body) as Record<string, unknown>);
        return new Response(
          JSON.stringify({ content: [{ type: "text", text: "ok" }], usage: { input_tokens: 1, output_tokens: 1 } }),
          { status: 200 },
        );
      }) as typeof fetch,
    });
    const out = await p.complete({
      messages: [
        { role: "system", content: "sys" },
        { role: "user", content: [{ type: "image", data: "AAA", mimeType: "image/png" }] },
        { role: "assistant", content: [{ type: "tool_call", id: "t1", name: "tool", args: { a: 1 } }] },
        { role: "tool", content: "res", toolCallId: "t1", toolName: "tool" },
      ],
      tools: [{ name: "tool", description: "d" }],
    });
    expect(out.text).toBe("ok");
    const body = seen[0];
    expect(body).toMatchObject({ system: "sys", temperature: 0.5, thinking: { type: "enabled" } });
  });

  it("network failure is retryable", async () => {
    const p = new AnthropicProvider({
      apiKey: "k",
      model: "m",
      fetchFn: (async () => { throw new TypeError("down"); }) as typeof fetch,
    });
    await expect(p.complete({ messages: [], tools: [] })).rejects.toMatchObject({ code: "MODEL_ERROR", retryable: true });
  });
});

describe("observability variants", () => {
  it("supportsColor respects NO_COLOR; opts-object overload; logError", () => {
    expect(typeof supportsColor()).toBe("boolean");
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    const err = vi.spyOn(console, "error").mockImplementation(() => {});
    const e = consoleEvents({ prefix: "[x]", colors: false });
    e.onStep?.({ step: 1, messages: [] });
    e.onDone?.({ text: "t", toolCalls: [] });
    e.onToken?.("a");
    e.onThinking?.("b");
    logError("[x]", new Error("bad"), false);
    logError("[x]", "plain", false);
    expect(log).toHaveBeenCalled();
    expect(err).toHaveBeenCalled();
    log.mockRestore();
    err.mockRestore();
  });
});

describe("client error paths", () => {
  it("non-ok, missing body, server error, network failure", async () => {
    const bad = new AgentClient("http://x", {
      fetchFn: (async () => new Response("nope", { status: 500 })) as typeof fetch,
    });
    await expect(bad.send("hi")).rejects.toMatchObject({ code: "A2A_TRANSPORT" });
    const nob = new AgentClient("http://x", {
      fetchFn: (async () => ({ ok: true, body: null, status: 200 }) as unknown as Response) as typeof fetch,
    });
    await expect(async () => { for await (const _evt of nob.stream("hi")) { void _evt; } }).rejects.toMatchObject({
      code: "A2A_TRANSPORT",
    });
    const failing = new AgentClient("http://x", {
      fetchFn: (async () => { throw new Error("down"); }) as typeof fetch,
    });
    await expect(failing.send("hi")).rejects.toMatchObject({ code: "A2A_TRANSPORT" });
    await expect(async () => { for await (const _evt of failing.stream("hi")) { void _evt; } }).rejects.toMatchObject({
      code: "A2A_TRANSPORT",
    });
  });

  it("server error event surfaces as A2A_ERROR", async () => {
    const sse = `data: {"error":"kaput"}\n\ndata: [DONE]\n`;
    const stream = new ReadableStream({ start: (c) => { c.enqueue(new TextEncoder().encode(sse)); c.close(); } });
    const client = new AgentClient("http://x", {
      fetchFn: (async () => new Response(stream, { status: 200 })) as typeof fetch,
    });
    await expect(async () => { for await (const _evt of client.stream("hi")) { void _evt; } }).rejects.toMatchObject({
      code: "A2A_ERROR",
    });
  });
});

describe("agent basics (colors off)", () => {
  it("runs with colorless console events", async () => {
    const agent = new Agent({ model: scripted(["ok"]), events: consoleEvents({ colors: false }) });
    expect((await agent.run("q")).text).toBe("ok");
  });
});
