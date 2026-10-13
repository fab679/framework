/**
 * Regression: token/thinking deltas stream LIVE — the first delta must be
 * yielded WHILE the provider call is still running (not batched until it
 * completes), and deltas from FAILED provider attempts must never leak into
 * the stream (the delivered-verdict contract: post-delta provider failures
 * are non-retryable so retries never duplicate already-streamed text).
 */
import { describe, expect, it } from "vitest";
import { Agent } from "../../src/agent/index.js";
import { modelRetryMiddleware } from "../../src/middleware/index.js";
import { OpenAICompatibleProvider } from "../../src/models/openai-compatible.js";
import type { CompleteOptions, ModelCompletion } from "../../src/models/index.js";
import { AgentError } from "../../src/types/index.js";

function sleep(ms: number) {
  return new Promise((r) => setTimeout(r, ms));
}

/** fetch-injectable SSE body: chunks are handed to the consumer one pull at a
 *  time, so earlier chunks are delivered before a later Error surfaces. */
function streamResponse(chunks: (string | Error)[]) {
  const encoder = new TextEncoder();
  let i = 0;
  const stream = new ReadableStream({
    pull(controller) {
      if (i >= chunks.length) { controller.close(); return; }
      const c = chunks[i++];
      if (c instanceof Error) controller.error(c);
      else controller.enqueue(encoder.encode(c));
    },
  });
  return new Response(stream, { status: 200 });
}

const SSE_CONTENT = 'data: {"choices":[{"delta":{"content":"tok"}}]}\n\n';

describe("live delta streaming", () => {
  it("yields the first delta while the provider call is still in flight", async () => {
    let finished = false;
    const provider = {
      id: "scripted-stream",
      capabilities: { structuredOutput: false, streaming: true },
      complete: async (opts: CompleteOptions): Promise<ModelCompletion> => {
        opts.onToken?.("early");
        await sleep(50);
        opts.onToken?.("-late");
        finished = true;
        return { text: "early-late", toolCalls: [], usage: { inputTokens: 1, outputTokens: 3 } };
      },
    };
    const agent = new Agent({ model: provider });
    const sawDeltaWhileInFlight: boolean[] = [];
    const iter = agent.stream("q");
    for (;;) {
      const next = await iter.next();
      if (next.done) {
        expect(next.value.text).toBe("early-late");
        break;
      }
      if (next.value.type === "token") {
        sawDeltaWhileInFlight.push(finished);
        if (next.value.delta === "early") expect(finished).toBe(false);
      }
    }
    expect(sawDeltaWhileInFlight).toContain(false); // first delta lived BEFORE completion
  });

  it("post-delta provider failure: no token events leak, error non-retryable, no retry (real stream guard)", async () => {
    let attempts = 0;
    const provider = new OpenAICompatibleProvider({
      baseUrl: "https://api.test/v1",
      apiKey: "k",
      model: "m",
      stream: true,
      fetchFn: (async () => {
        attempts += 1;
        return streamResponse([
          SSE_CONTENT,
          new Error("stream reset after content"),
        ]);
      }) as typeof fetch,
    });
    const agent = new Agent({
      model: provider,
      middleware: [modelRetryMiddleware({ maxRetries: 2, initialDelayMs: 1 })],
    });
    const tokens: string[] = [];
    const run = async () => {
      const iter = agent.stream("q");
      for (;;) {
        const next = await iter.next();
        if (next.done) return next.value;
        if (next.value.type === "token") tokens.push(next.value.delta);
      }
    };
    // The delivered-verdict stamps post-delta failures non-retryable, so
    // model_retry never re-runs the attempt (a retry would duplicate "tok").
    await expect(run()).rejects.toMatchObject({ retryable: false });
    expect(tokens).toEqual([]);
    expect(attempts).toBe(1);
  });

  it("pre-delta provider failure: model_retry retries and the second attempt streams cleanly", async () => {
    let attempts = 0;
    const tokens: string[] = [];
    const provider = {
      id: "flaky-pre-delta",
      capabilities: { structuredOutput: false, streaming: true },
      complete: async (opts: CompleteOptions): Promise<ModelCompletion> => {
        attempts += 1;
        if (attempts === 1) {
          // BEFORE any onToken: nothing was delivered, so a retry is safe.
          throw new AgentError("MODEL_ERROR", "connection refused", true);
        }
        await sleep(5);
        opts.onToken?.("clean");
        return { text: "clean", toolCalls: [], usage: { inputTokens: 1, outputTokens: 6 } };
      },
    };
    const agent = new Agent({ model: provider, middleware: [modelRetryMiddleware({ maxRetries: 2, initialDelayMs: 1 })] });
    const iter = agent.stream("q");
    for (;;) {
      const next = await iter.next();
      if (next.done) {
        expect(next.value.text).toBe("clean");
        break;
      }
      if (next.value.type === "token") tokens.push(next.value.delta);
    }
    expect(attempts).toBe(2);
    expect(tokens).toEqual(["clean"]);
  });

  it("rolls back batched deltas of a failed attempt even when the provider stamps it retryable", async () => {
    let attempts = 0;
    const tokens: string[] = [];
    const provider = {
      id: "scripted-poison",
      capabilities: { structuredOutput: false, streaming: true },
      complete: async (opts: CompleteOptions): Promise<ModelCompletion> => {
        attempts += 1;
        if (attempts === 1) {
          opts.onToken?.("poison"); // must NEVER reach the stream
          throw new AgentError("MODEL_ERROR", "mid-stream reset", true);
        }
        await sleep(5);
        opts.onToken?.("clean");
        return { text: "clean", toolCalls: [], usage: { inputTokens: 1, outputTokens: 6 } };
      },
    };
    const agent = new Agent({ model: provider, middleware: [modelRetryMiddleware({ maxRetries: 2, initialDelayMs: 1 })] });
    const iter = agent.stream("q");
    for (;;) {
      const next = await iter.next();
      if (next.done) break;
      if (next.value.type === "token") tokens.push(next.value.delta);
    }
    expect(attempts).toBe(2);
    expect(tokens).toEqual(["clean"]);
  });
});