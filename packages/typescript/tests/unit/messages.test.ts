import { describe, expect, it } from "vitest";
import {
  assistant,
  deserializeMessages,
  hasImages,
  imagesOf,
  serializeMessages,
  system,
  textOf,
  toolResult,
  user,
} from "../../src/types/index.js";

describe("content blocks", () => {
  it("textOf reads strings and blocks", () => {
    expect(textOf("hi")).toBe("hi");
    expect(
      textOf([
        { type: "text", text: "a" },
        { type: "reasoning", reasoning: "hmm" },
        { type: "image", url: "https://ex.com/a.png" },
      ]),
    ).toBe("ahmm");
  });

  it("hasImages detects image blocks only", () => {
    expect(hasImages("plain")).toBe(false);
    expect(hasImages([{ type: "text", text: "t" }])).toBe(false);
    expect(hasImages([{ type: "image", url: "https://ex.com/a.png" }])).toBe(true);
  });

  it("covers non-text/tool blocks and imagesOf shapes", () => {
    expect(textOf([{ type: "tool_call", id: "c", name: "t", args: {} }])).toBe("");
    expect(textOf([{ type: "non_standard", value: 1 }])).toBe("");
    expect(imagesOf("plain")).toEqual([]);
    expect(imagesOf([{ type: "image", data: "AAA", mimeType: "image/png" }])).toEqual([{ url: undefined, data: "AAA", mimeType: "image/png" }]);
  });
});

describe("message factories + ids", () => {
  it("assigns unique ids and round-trips tool_call_id", () => {
    const s = system("be nice");
    const u = user([{ type: "text", text: "hi" }], { name: "ada" });
    const t = toolResult("call_1", "get_time", "noon");
    expect(new Set([s.id, u.id, t.id]).size).toBe(3);
    expect(t.toolCallId).toBe("call_1");
    expect(assistant("ok").role).toBe("assistant");
  });

  it("serializes and validates history", () => {
    const msgs = [system("s"), user("hi"), toolResult("call_1", "t", "out")];
    const restored = deserializeMessages(serializeMessages(msgs));
    expect(restored).toEqual(msgs);
    expect(() => deserializeMessages([{ role: "bogus", content: "x" }])).toThrow();
  });
});
