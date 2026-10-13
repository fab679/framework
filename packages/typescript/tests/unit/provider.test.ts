import { describe, expect, it } from "vitest";
import { OpenAICompatibleProvider } from "../../src/models/openai-compatible.js";
import { AnthropicProvider } from "../../src/models/anthropic.js";
import { __resetConfig, loadConfig } from "../../src/config/index.js";

function mockFetch(payload: unknown) {
  return (async () =>
    new Response(JSON.stringify(payload), { status: 200, headers: { "content-type": "application/json" } })) as typeof fetch;
}

describe("OpenAICompatibleProvider (offline, mocked fetch)", () => {
  it("parses text + usage", async () => {
    const p = new OpenAICompatibleProvider({
      baseUrl: "https://api.together.xyz/v1",
      apiKey: "test-key",
      model: "mock-model",
      fetchFn: mockFetch({
        choices: [{ message: { content: "hello", tool_calls: [] } }],
        usage: { prompt_tokens: 7, completion_tokens: 3 },
      }),
    });
    const out = await p.complete({ messages: [{ role: "user", content: "hi" }], tools: [] });
    expect(out.text).toBe("hello");
    expect(out.usage).toEqual({ inputTokens: 7, outputTokens: 3 });
  });

  it("parses tool calls with JSON args", async () => {
    const p = new OpenAICompatibleProvider({
      baseUrl: "https://api.together.xyz/v1",
      apiKey: "test-key",
      model: "mock-model",
      fetchFn: mockFetch({
        choices: [
          { message: { content: "", tool_calls: [{ function: { name: "get_time", arguments: "{}" } }] } },
        ],
        usage: { prompt_tokens: 1, completion_tokens: 1 },
      }),
    });
    const out = await p.complete({
      messages: [{ role: "user", content: "time?" }],
      tools: [{ name: "get_time", description: "clock" }],
    });
    expect(out.toolCalls).toEqual([{ id: expect.any(String), name: "get_time", args: {} }]);
  });

  it("throws without apiKey", () => {
    expect(() => new OpenAICompatibleProvider({ baseUrl: "https://x", apiKey: "", model: "m" })).toThrow();
  });
});

describe("loadConfig", () => {
  it("prefers TogetherAI when key set", () => {
    __resetConfig();
    const cfg = loadConfig({ TOGETHER_API_KEY: "tk", TOGETHER_MODEL: "llama" } as NodeJS.ProcessEnv);
    expect(cfg.active.source).toBe("together");
    expect(cfg.active.model).toBe("llama");
    __resetConfig();
  });

  it("falls back to OpenAI shape otherwise", () => {
    __resetConfig();
    const cfg = loadConfig({} as NodeJS.ProcessEnv);
    expect(cfg.active.source).toBe("openai");
    __resetConfig();
  });
});

describe("capabilities", () => {
  it("rejects image blocks on a text-only model with MODEL_CAPABILITY", async () => {
    const p = new OpenAICompatibleProvider({
      baseUrl: "https://api.together.xyz/v1",
      apiKey: "test-key",
      model: "Qwen/Qwen3.5-9B",
      fetchFn: mockFetch({ choices: [{ message: { content: "x" } }] }),
    });
    await expect(
      p.complete({
        messages: [{ role: "user", content: [{ type: "image", url: "https://ex.com/a.png" }] }],
        tools: [],
      }),
    ).rejects.toMatchObject({ name: "AgentError", code: "MODEL_CAPABILITY" });
  });
});

describe("thinking stream (offline, mocked SSE)", () => {
  it("routes reasoning_content to onThinking, content to onToken", async () => {
    const sse =
      `data: {"choices":[{"delta":{"reasoning_content":"let me think"}}]}\n\n` +
      `data: {"choices":[{"delta":{"content":"ok"}}]}\n\n` +
      `data: [DONE]\n`;
    const stream = new ReadableStream({ start: (c) => { c.enqueue(new TextEncoder().encode(sse)); c.close(); } });
    const p = new OpenAICompatibleProvider({
      baseUrl: "https://api.together.xyz/v1",
      apiKey: "test-key",
      model: "mock-model",
      stream: true,
      fetchFn: (async () => new Response(stream, { status: 200 })) as typeof fetch,
    });
    const tokens: string[] = [];
    const thoughts: string[] = [];
    const out = await p.complete({
      messages: [{ role: "user", content: "hi" }],
      tools: [],
      onToken: (d) => tokens.push(d),
      onThinking: (d) => thoughts.push(d),
    });
    expect(out.text).toBe("ok");
    expect(tokens).toEqual(["ok"]);
    expect(thoughts).toEqual(["let me think"]);
  });

  it("keeps working when event/data lines split across chunks", async () => {
    const sse =
      `event: content_block_start\ndata: {"content_block":{"type":"text"}}\n\n` +
      `event: content_block_delta\ndata: {"delta":{"type":"text_delta","text":"hi"}}\n\n` +
      `event: content_block_delta\ndata: {"delta":{"type":"text_delta","text":" there"}}\n\n` +
      `event: content_block_stop\ndata: {}\n\n` +
      `event: message_start\ndata: {"message":{"usage":{"input_tokens":11}}}\n\n` +
      `event: message_delta\ndata: {"usage":{"output_tokens":22}}\n\n`;
    // Split at arbitrary byte boundaries — `event:` and `data:` of the same
    // event must land in different chunks (regression: flush() used to reset
    // the parsed event name per chunk, silently dropping the delta).
    const bytes = new TextEncoder().encode(sse);
    const stream = new ReadableStream({
      start: (c) => {
        for (let i = 0; i < bytes.length; i += 7) {
          c.enqueue(bytes.slice(i, i + 7));
        }
        c.close();
      },
    });
    const p = new AnthropicProvider({
      apiKey: "test-key",
      model: "claude-mock",
      stream: true,
      fetchFn: (async () => new Response(stream, { status: 200 })) as typeof fetch,
    });
    const out = await p.complete({ messages: [{ role: "user", content: "hi" }], tools: [] });
    expect(out.text).toBe("hi there");
    expect(out.usage).toEqual({ inputTokens: 11, outputTokens: 22 });
  });
});

describe("OpenAI stream usage (offline, mocked SSE)", () => {
  it("captures usage from the final chunk and requests include_usage", async () => {
    let sentBody = "";
    const sse =
      `data: {"choices":[{"delta":{"content":"hello"}}]}\n\n` +
      `data: {"choices":[],"usage":{"prompt_tokens":9,"completion_tokens":4,"completion_tokens_details":{"reasoning_tokens":3}}}\n\n` +
      `data: [DONE]\n`;
    const stream = new ReadableStream({ start: (c) => { c.enqueue(new TextEncoder().encode(sse)); c.close(); } });
    const p = new OpenAICompatibleProvider({
      baseUrl: "https://api.together.xyz/v1",
      apiKey: "test-key",
      model: "mock-model",
      stream: true,
      fetchFn: (async (_url: Parameters<typeof fetch>[0], init?: RequestInit) => {
        sentBody = String(init?.body ?? "");
        return new Response(stream, { status: 200 });
      }) as typeof fetch,
    });
    const out = await p.complete({ messages: [{ role: "user", content: "hi" }], tools: [] });
    expect(out.usage).toEqual({ inputTokens: 9, outputTokens: 4, reasoningTokens: 3 });
    expect(JSON.parse(sentBody).stream_options).toEqual({ include_usage: true });
  });
});

describe("silent truncation guard (thinking models that burn the whole budget)", () => {
  it("throws MODEL_ERROR when finish_reason=length produced NO content", async () => {
    const p = new OpenAICompatibleProvider({
      baseUrl: "https://api.together.xyz/v1",
      apiKey: "k",
      model: "m",
      fetchFn: mockFetch({ choices: [{ message: { content: "" }, finish_reason: "length" }], usage: { prompt_tokens: 5, completion_tokens: 300 } }),
    });
    await expect(p.complete({ messages: [{ role: "user", content: "hi" }], tools: [] })).rejects.toMatchObject({
      code: "MODEL_ERROR",
      retryable: false,
    });
  });

  it("keeps partial-but-present content, reporting finishReason", async () => {
    const p = new OpenAICompatibleProvider({
      baseUrl: "https://api.together.xyz/v1",
      apiKey: "k",
      model: "m",
      fetchFn: mockFetch({ choices: [{ message: { content: "half an ans" }, finish_reason: "length" }], usage: { prompt_tokens: 5, completion_tokens: 300 } }),
    });
    const out = await p.complete({ messages: [{ role: "user", content: "hi" }], tools: [] });
    expect(out.text).toBe("half an ans");
    expect(out.finishReason).toBe("length");
  });

  it("stream variant: rejects finish_reason=length with zero content and no tool calls", async () => {
    const sse =
      `data: {"choices":[{"delta":{"reasoning":"hmm"}}]}\n\n` +
      `data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n\n` +
      `data: [DONE]\n`;
    const stream = new ReadableStream({ start: (c) => { c.enqueue(new TextEncoder().encode(sse)); c.close(); } });
    const p = new OpenAICompatibleProvider({
      baseUrl: "https://api.together.xyz/v1",
      apiKey: "k",
      model: "m",
      stream: true,
      fetchFn: (async () => new Response(stream, { status: 200 })) as typeof fetch,
    });
    await expect(p.complete({ messages: [{ role: "user", content: "hi" }], tools: [] })).rejects.toMatchObject({
      code: "MODEL_ERROR",
    });
  });
});

describe("reasoning-only stop guard (whole answer landed in the reasoning channel)", () => {
  it("throws retryable MODEL_ERROR when stop produced ONLY thinking", async () => {
    const p = new OpenAICompatibleProvider({
      baseUrl: "https://api.together.xyz/v1",
      apiKey: "k",
      model: "m",
      fetchFn: mockFetch({
        choices: [{ message: { content: "", reasoning: "the whole answer is here" }, finish_reason: "stop" }],
        usage: { prompt_tokens: 5, completion_tokens: 30 },
      }),
    });
    await expect(p.complete({ messages: [{ role: "user", content: "hi" }], tools: [] })).rejects.toMatchObject({
      code: "MODEL_ERROR",
      retryable: true,
    });
  });

  it("keeps the completion when thinking AND content both present on stop", async () => {
    const p = new OpenAICompatibleProvider({
      baseUrl: "https://api.together.xyz/v1",
      apiKey: "k",
      model: "m",
      fetchFn: mockFetch({
        choices: [{ message: { content: "42", reasoning: "because math" }, finish_reason: "stop" }],
        usage: { prompt_tokens: 5, completion_tokens: 12 },
      }),
    });
    const out = await p.complete({ messages: [{ role: "user", content: "hi" }], tools: [] });
    expect(out.text).toBe("42");
    expect(out.thinking).toBe("because math");
  });

  it("stream variant: thinking-only stop is a retryable MODEL_ERROR", async () => {
    const sse =
      `data: {"choices":[{"delta":{"reasoning":"all in reasoning"}}]}\n\n` +
      `data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n` +
      `data: [DONE]\n`;
    const stream = new ReadableStream({ start: (c) => { c.enqueue(new TextEncoder().encode(sse)); c.close(); } });
    const p = new OpenAICompatibleProvider({
      baseUrl: "https://api.together.xyz/v1",
      apiKey: "k",
      model: "m",
      stream: true,
      fetchFn: (async () => new Response(stream, { status: 200 })) as typeof fetch,
    });
    await expect(p.complete({ messages: [{ role: "user", content: "hi" }], tools: [] })).rejects.toMatchObject({
      code: "MODEL_ERROR",
      retryable: true,
    });
  });

  it("anthropic readJson: thinking-only stop is a retryable MODEL_ERROR", async () => {
    const p = new AnthropicProvider({
      apiKey: "k",
      model: "claude-mock",
      fetchFn: mockFetch({
        content: [{ type: "thinking", thinking: "the whole answer lived here" }],
        stop_reason: "stop",
        usage: { input_tokens: 5, output_tokens: 40 },
      }),
    });
    await expect(p.complete({ messages: [{ role: "user", content: "hi" }], tools: [] })).rejects.toMatchObject({
      code: "MODEL_ERROR",
      retryable: true,
    });
  });

  it("anthropic readStream: thinking-only stop is rejected", async () => {
    const sse =
      `event: content_block_start\ndata: {"content_block":{"type":"thinking"}}\n\n` +
      `event: content_block_delta\ndata: {"delta":{"thinking":"all in reasoning"}}\n\n` +
      `event: message_delta\ndata: {"usage":{"output_tokens":40},"delta":{"stop_reason":"stop"}}\n\n`;
    const stream = new ReadableStream({ start: (c) => { c.enqueue(new TextEncoder().encode(sse)); c.close(); } });
    const p = new AnthropicProvider({
      apiKey: "k",
      model: "claude-mock",
      stream: true,
      fetchFn: (async () => new Response(stream, { status: 200 })) as typeof fetch,
    });
    await expect(p.complete({ messages: [{ role: "user", content: "hi" }], tools: [] })).rejects.toMatchObject({
      code: "MODEL_ERROR",
      retryable: true,
    });
  });
});
