import { describe, expect, it } from "vitest";
import { Agent } from "../../src/agent/index.js";
import {
  NullConversationManager,
  SlidingWindowConversationManager,
  SummarizingConversationManager,
  historyTokens,
  trimDangling,
} from "../../src/conversation/index.js";
import type { ChatMessage, ContentBlock } from "../../src/types/index.js";

function textReply(text: string) {
  return {
    id: "t",
    complete: async () => ({ text, toolCalls: [], usage: { inputTokens: 1, outputTokens: 1 } }),
  };
}

const msg = (role: ChatMessage["role"], content: string | ContentBlock[], extra?: Partial<ChatMessage>): ChatMessage => ({ role, content, ...extra });

describe("trimDangling + tokens", () => {
  it("drops leading orphan tool/call-only messages, keeps pinned", () => {
    const msgs = [
      msg("tool", "orphan", { toolCallId: "c", toolName: "t" }),
      msg("assistant", [{ type: "tool_call", id: "c2", name: "t", args: {} }]),
      msg("user", "hello"),
    ];
    expect(trimDangling(msgs).map((m) => m.role)).toEqual(["user"]);
    const pinned = [msg("tool", "keep", { toolCallId: "c", toolName: "t", pinned: true }), msg("user", "hi")];
    expect(trimDangling(pinned)).toHaveLength(2);
  });

  it("historyTokens estimates", () => {
    expect(historyTokens([msg("user", "abcd")])).toBe(1);
  });
});

describe("managers", () => {
  it("null keeps everything", () => {
    const m = new NullConversationManager();
    const msgs = [msg("user", "a")];
    expect(m.apply(msgs)).toBe(msgs);
    expect(m.reduce(msgs)).toBe(msgs);
  });

  it("sliding window keeps newest + pins first", () => {
    const m = new SlidingWindowConversationManager({ windowSize: 2, pinFirst: 1 });
    const msgs = [msg("system", "sys"), msg("user", "1"), msg("user", "2"), msg("user", "3")];
    const out = m.apply(msgs);
    expect(out.map((x) => x.content)).toEqual(["sys", "2", "3"]);
    expect(m.removedCount).toBe(1);
  });

  it("sliding perTurn trims inside the loop", async () => {
    const agent = new Agent({
      model: textReply("ok"),
      conversationManager: new SlidingWindowConversationManager({ windowSize: 100, perTurn: true }),
    });
    const res = await agent.run("hi");
    expect(res.text).toBe("ok");
  });

  it("summarizing compresses old turns", async () => {
    const m = new SummarizingConversationManager({ summaryRatio: 0.5, preserveRecent: 1, summarizer: async (h) => `n=${h.length}` });
    const msgs = [msg("system", "s"), msg("user", "1"), msg("user", "2"), msg("user", "3"), msg("user", "4")];
    const out = await m.apply(msgs);
    expect(out.length).toBeLessThan(msgs.length);
    expect(String(out[0].content)).toContain("n=");
    expect(m.lastSummary).toContain("n=");
  });

  it("summarizing is a no-op when nothing to compress", async () => {
    const m = new SummarizingConversationManager({ summarizer: async () => "s" });
    const msgs = [msg("user", "hi")];
    expect(await m.apply(msgs)).toBe(msgs);
  });
});
