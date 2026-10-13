import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { Agent } from "../../src/agent/index.js";
import { FileMemory, InMemoryMemory } from "../../src/memory/index.js";
import { CompactedMemory } from "../../src/memory/compaction.js";
import { estimateCostUsd, estimateTokens } from "../../src/utils/index.js";
import type { ModelCompletion } from "../../src/models/index.js";

function textReply(text: string, usage: ModelCompletion["usage"] = { inputTokens: 10, outputTokens: 5 }) {
  return {
    id: "t",
    complete: async (): Promise<ModelCompletion> => ({ text, toolCalls: [], usage }),
  };
}

describe("tokenizer", () => {
  it("counts words, punctuation, CJK, numbers", () => {
    expect(estimateTokens("")).toBe(0);
    expect(estimateTokens("abcd")).toBe(1);
    expect(estimateTokens("abcdefgh")).toBe(2);
    expect(estimateTokens("hello world")).toBe(2);
    expect(estimateTokens("Hello, world!")).toBe(4);
    expect(estimateTokens("日本語")).toBe(3);
    expect(estimateCostUsd({ inputTokens: 1_000_000, outputTokens: 1_000_000 }, { inputPerMillion: 1, outputPerMillion: 3 })).toBe(4);
  });
});

describe("FileMemory", () => {
  it("persists across instances, windows, validates reloads", async () => {
    const dir = mkdtempSync(join(tmpdir(), "mem-"));
    const a = new FileMemory(dir, 3);
    await a.save([{ role: "user", content: "1" }, { role: "user", content: "2" }]);
    const b = new FileMemory(dir, 3);
    expect((await b.load()).map((m) => m.content)).toEqual(["1", "2"]);
    await b.save([{ role: "user", content: "3" }, { role: "user", content: "4" }]);
    expect((await b.load()).map((m) => m.content)).toEqual(["2", "3", "4"]);
    expect(JSON.parse(readFileSync(join(dir, "memory.json"), "utf8"))).toHaveLength(3);
    await b.clear();
    expect(await b.load()).toEqual([]);
  });
});

describe("budgets", () => {
  it("caps tokens, cost, and deadline", async () => {
    const hungry = textReply("x", { inputTokens: 100, outputTokens: 100 });
    await expect(new Agent({ model: hungry, budgets: { maxTokens: 50 } }).run("q")).rejects.toMatchObject({
      code: "BUDGET_EXCEEDED",
    });
    await expect(
      new Agent({ model: hungry, budgets: { maxCostUsd: 0.00001, prices: { inputPerMillion: 10, outputPerMillion: 10 } } }).run("q"),
    ).rejects.toMatchObject({ code: "BUDGET_EXCEEDED" });
    await expect(new Agent({ model: textReply("x"), budgets: { deadlineMs: -1 } }).run("q")).rejects.toMatchObject({
      code: "BUDGET_EXCEEDED",
    });
    const ok = await new Agent({
      model: textReply("x"),
      budgets: { maxTokens: 1000, maxCostUsd: 1, deadlineMs: 5000, prices: { inputPerMillion: 1, outputPerMillion: 1 } },
    }).run("q");
    expect(ok.text).toBe("x");
  });

  it("refuses maxCostUsd without a price and reports costUsd when known", async () => {
    const prices = { inputPerMillion: 1, outputPerMillion: 3 };
    await expect(new Agent({ model: textReply("x"), budgets: { maxCostUsd: 1 } }).run("q")).rejects.toMatchObject({
      code: "BUDGET_NO_PRICE",
    });
    const run = await new Agent({ model: textReply("x", { inputTokens: 1_000_000, outputTokens: 1_000_000 }), budgets: { prices } }).run("q");
    expect(run.usage.costUsd).toBeCloseTo(4);
    // Unknown model without prices: cost stays undefined, never zero.
    const unknown = await new Agent({ model: textReply("x") }).run("q");
    expect(unknown.usage.costUsd).toBeUndefined();
  });
});

describe("compaction fidelity", () => {
  it("summary boundary preserves recency and the summary mentions overflow", async () => {
    const mem = new CompactedMemory(new InMemoryMemory(), { maxMessages: 3 });
    await mem.save([
      { role: "user", content: "alpha" },
      { role: "user", content: "beta" },
      { role: "user", content: "gamma" },
      { role: "user", content: "delta" },
    ]);
    const loaded = await mem.load();
    expect(loaded.length).toBeLessThanOrEqual(3);
    expect(loaded.at(-1)?.content).toBe("delta");
    expect(String(loaded[0].content)).toContain("Prior context summary");
  });

  it("custom summarizer keeps triples, drops prose (semantic variant)", async () => {
    const mem = new CompactedMemory(new InMemoryMemory(), {
      maxMessages: 2,
      summarize: async (msgs) => `FACTS: ${msgs.map((m) => String(m.content).split(" ")[0]).join(",")}`,
    });
    await mem.save([
      { role: "user", content: "ada worksFor acme" },
      { role: "user", content: "bo likes tea" },
      { role: "user", content: "latest" },
    ]);
    const loaded = await mem.load();
    expect(String(loaded[0].content)).toBe("FACTS: ada,bo");
    expect(loaded.at(-1)?.content).toBe("latest");
  });
});
