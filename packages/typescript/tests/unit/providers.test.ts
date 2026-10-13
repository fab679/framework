import { describe, expect, it } from "vitest";
import { AnthropicProvider } from "../../src/models/anthropic.js";
import { ollama } from "../../src/models/ollama.js";
import { initChatModel } from "../../src/models/factory.js";
import { __resetConfig } from "../../src/config/index.js";

function jsonFetch(payload: unknown, status = 200) {
  return (async () =>
    new Response(JSON.stringify(payload), { status, headers: { "content-type": "application/json" } })) as typeof fetch;
}

describe("AnthropicProvider (offline, mocked)", () => {
  it("parses text + thinking + tool_use + usage", async () => {
    const p = new AnthropicProvider({
      apiKey: "k",
      model: "claude-sonnet-4-6",
      fetchFn: jsonFetch({
        content: [
          { type: "thinking", thinking: "hmm" },
          { type: "text", text: "hi" },
          { type: "tool_use", id: "toolu_1", name: "get_time", input: {} },
        ],
        usage: { input_tokens: 9, output_tokens: 4 },
      }),
    });
    const tokens: string[] = [];
    const thoughts: string[] = [];
    const out = await p.complete({
      messages: [{ role: "user", content: "hi" }],
      tools: [{ name: "get_time", description: "clock" }],
      onToken: (d) => tokens.push(d),
      onThinking: (d) => thoughts.push(d),
    });
    expect(out.text).toBe("hi");
    expect(out.thinking).toBe("hmm");
    expect(out.toolCalls).toEqual([{ id: "toolu_1", name: "get_time", args: {} }]);
    expect(out.usage).toEqual({ inputTokens: 9, outputTokens: 4 });
    expect(tokens).toEqual(["hi"]);
    expect(thoughts).toEqual(["hmm"]);
  });

  it("maps errors with retryable flags", async () => {
    const p = (status: number) =>
      new AnthropicProvider({ apiKey: "k", model: "m", fetchFn: jsonFetch({ error: { message: "x" } }, status) });
    await expect(p(500).complete({ messages: [], tools: [] })).rejects.toMatchObject({ code: "MODEL_ERROR", retryable: true });
    await expect(p(401).complete({ messages: [], tools: [] })).rejects.toMatchObject({ code: "MODEL_ERROR", retryable: false });
  });

  it("throws without apiKey; streams SSE deltas", async () => {
    expect(() => new AnthropicProvider({ apiKey: "", model: "m" })).toThrow();
    const sse =
      `event: content_block_start\ndata: {"content_block":{"type":"thinking"}}\n\n` +
      `event: content_block_delta\ndata: {"delta":{"type":"thinking_delta","thinking":"ponder"}}\n\n` +
      `event: content_block_stop\ndata: {}\n\n` +
      `event: content_block_start\ndata: {"content_block":{"type":"text"}}\n\n` +
      `event: content_block_delta\ndata: {"delta":{"type":"text_delta","text":"yo"}}\n\n` +
      `event: content_block_stop\ndata: {}\n\n`;
    const stream = new ReadableStream({ start: (c) => { c.enqueue(new TextEncoder().encode(sse)); c.close(); } });
    const p = new AnthropicProvider({
      apiKey: "k",
      model: "m",
      stream: true,
      fetchFn: (async () => new Response(stream, { status: 200 })) as typeof fetch,
    });
    const out = await p.complete({ messages: [{ role: "user", content: "q" }], tools: [] });
    expect(out.text).toBe("yo");
    expect(out.thinking).toBe("ponder");
  });
});

describe("ollama()", () => {
  it("targets localhost with no key", () => {
    const p = ollama({ model: "qwen2.5" });
    expect(p.id).toBe("ollama:qwen2.5");
    expect(p.capabilities?.structuredOutput).toBe(false);
  });
});

describe("initChatModel", () => {
  it("resolves anthropic + ollama + errors", () => {
    __resetConfig();
    const a = initChatModel("anthropic:claude-sonnet-4-6", { apiKey: "k" });
    expect(a.id).toBe("anthropic:claude-sonnet-4-6");
    const o = initChatModel("ollama:llama3.1");
    expect(o.id).toBe("ollama:llama3.1");
    expect(() => initChatModel("anthropic:claude-x")).toThrow(/ANTHROPIC_API_KEY/);
    expect(() => initChatModel("wat:model", { apiKey: "k" })).toThrow(/unknown provider/);
    __resetConfig();
  });
});
