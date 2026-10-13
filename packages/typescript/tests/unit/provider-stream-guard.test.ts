/** Mid-stream retry-verdict rule: after deltas reach the caller, a broken
 * stream is NON-retryable (re-runs would duplicate text); before any delta,
 * failures stay retryable. */
import { describe, expect, it } from "vitest";
import { OpenAICompatibleProvider } from "../../src/models/openai-compatible.js";

function streamResponse(chunks: (string | Error)[]) {
  const encoder = new TextEncoder();
  let i = 0;
  // pull()-based so the error only lands on the read AFTER earlier chunks
  // were handed to the consumer (erroring the controller discards anything
  // still queued, which would hide the delivery the guard must verdict on).
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

function provider(chunks: (string | Error)[]) {
  return new OpenAICompatibleProvider({
    baseUrl: "https://api.test/v1", apiKey: "k", model: "m", stream: true,
    fetchFn: (async () => streamResponse(chunks)) as typeof fetch,
  });
}

describe("stream retry-verdict rule", () => {
  it("error BEFORE any delta: retryable (nothing was delivered)", async () => {
    const p = provider([new Error("connection reset")]);
    await expect(p.complete({ messages: [] })).rejects.toMatchObject({ retryable: true });
  });
  it("error AFTER deltas: non-retryable (re-runs would duplicate)", async () => {
    const p = provider([
      'data: {"choices":[{"delta":{"content":"tok"}}]}\n\n',
      new Error("stream reset after content"),
    ]);
    await expect(p.complete({ messages: [] })).rejects.toMatchObject({ retryable: false });
  });
});
