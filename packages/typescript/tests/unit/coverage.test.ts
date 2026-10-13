import { describe, expect, it, vi } from "vitest";
import { sleep, estimateTokens } from "../../src/utils/index.js";
import { consoleEvents } from "../../src/observability/index.js";
import { runOrchestrated } from "../../src/orchestration/index.js";
import { runWorkflow } from "../../src/workflows/index.js";
import { defaultToResult, ToolRegistry, defineTool } from "../../src/tools/index.js";
import { parseSkillFile, SkillRegistry } from "../../src/skills/index.js";
import { InMemoryGraphStore } from "../../src/semantic/index.js";
import { OpenAICompatibleProvider, providerFromEnv } from "../../src/models/openai-compatible.js";
import { runWithBudget } from "../../src/runtime/index.js";
import { z } from "zod";

describe("utils", () => {
  it("sleep resolves fast and aborts", async () => {
    await sleep(0);
    await sleep(5);
    const c = new AbortController();
    c.abort();
    await expect(sleep(1000, c.signal)).rejects.toThrow();
  });

  it("estimateTokens scales with length", () => {
    expect(estimateTokens("")).toBe(0);
    expect(estimateTokens("abcd")).toBe(1);
    expect(estimateTokens("abcdefgh")).toBe(2);
  });
});

describe("observability", () => {
  it("consoleEvents logs without throwing", () => {
    const log = vi.spyOn(console, "log").mockImplementation(() => {});
    const e = consoleEvents();
    e.onStep?.({ step: 0, messages: [] });
    e.onToolCall?.({ tool: "t", args: {} });
    e.onToolResult?.({ tool: "t", output: 1 });
    e.onDone?.({ text: "done", toolCalls: [] });
    expect(log).toHaveBeenCalled();
    log.mockRestore();
  });
});

describe("teams + workflows", () => {
  it("empty team and empty workflow behave", async () => {
    await expect(runOrchestrated({ strategy: "sequential", agents: [] }, "x")).rejects.toMatchObject({
      code: "ORCHESTRATION_EMPTY",
    });
    await expect(runWorkflow([])).resolves.toEqual({});
  });
});

describe("registries", () => {
  it("defaultToResult handles strings and objects", () => {
    expect(defaultToResult("x")).toEqual({ content: "x" });
    expect(defaultToResult({ a: 1 })).toEqual({ content: '{"a":1}' });
  });

  it("ToolRegistry duplicate + get/list", () => {
    const reg = new ToolRegistry();
    const t = defineTool({ name: "t", description: "t", schema: z.object({}), execute: async () => 1 });
    reg.register(t);
    expect(() => reg.register(t)).toThrow();
    expect(reg.get("t")).toBe(t);
    expect(reg.get("missing")).toBeUndefined();
    expect(reg.list()).toHaveLength(1);
  });

  it("SkillRegistry get/asTools + parse errors", () => {
    const reg = new SkillRegistry();
    expect(reg.get("missing")).toBeUndefined();
    expect(reg.asTools()).toEqual([]);
    const s = parseSkillFile(`---\nname: s\ndescription: d\n---\nbody`, "s/SKILL.md");
    reg.register({ ...s, tools: [defineTool({ name: "st", description: "d", schema: z.object({}), execute: async () => 1 })] });
    expect(reg.asTools()).toHaveLength(1);
    expect(() => parseSkillFile("no frontmatter", "x")).toThrow();
    expect(() => parseSkillFile(`---\nname: s\ndescription: d\n---\n`, "x")).toThrow();
  });

  it("GraphStore match filters + why stub", async () => {
    const store = new InMemoryGraphStore();
    await store.add([{ subject: "a", predicate: "b", object: "c", source: "s" }]);
    expect(await store.match(null, "b", null)).toHaveLength(1);
    expect(await store.match("zzz", null, null)).toHaveLength(0);
    expect(await store.why({ subject: "a", predicate: "b", object: "c" })).toBeNull();
  });

  it("providerFromEnv builds", () => {
    expect(providerFromEnv({ baseUrl: "https://x", apiKey: "k", model: "m" }).id).toContain("m");
  });

  it("runWithBudget passes through without guards", async () => {
    await expect(runWithBudget(async () => "ok")).resolves.toBe("ok");
  });

  it("runWithBudget normalizes mid-flight abort", async () => {
    const c = new AbortController();
    const p = runWithBudget(() => new Promise(() => {}), { signal: c.signal, timeoutMs: 5000 });
    c.abort();
    await expect(p).rejects.toMatchObject({ code: "ABORTED" });
  });

  it("defineTool rejects empty names; registry handles non-Error zod throws", async () => {
    expect(() => defineTool({ name: "", description: "d", schema: z.object({}), execute: async () => 1 })).toThrow();
    const reg = new ToolRegistry().register(
      defineTool({
        name: "weird",
        description: "d",
        schema: { parse: () => { throw "string-failure"; } } as unknown as z.ZodTypeAny,
        execute: async () => 1,
      }),
    );
    await expect(reg.call("weird", {})).rejects.toMatchObject({ code: "TOOL_ARG_INVALID" });
  });

  it("skill without tools contributes nothing; invalid add throws SHACL", async () => {
    const reg = new SkillRegistry();
    const s = parseSkillFile(`---\nname: bare\ndescription: d\n---\nbody`, "bare/SKILL.md");
    reg.register(s);
    expect(reg.asTools()).toEqual([]);
    const store = new InMemoryGraphStore();
    await expect(store.add([{ subject: "", predicate: "b", object: "c", source: "s" }])).rejects.toThrow(/SHACL/);
  });

  it("stream assembles split tool-call chunks; malformed args fall back to {}", async () => {
    const sse =
      `data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_9","function":{"name":"add","arguments":"{\\"a\\":"}}]}}]}\n\n` +
      `data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":" broken"}}]}}]}\n\n` +
      `data: [DONE]\n`;
    const stream = new ReadableStream({ start: (c) => { c.enqueue(new TextEncoder().encode(sse)); c.close(); } });
    const p = new OpenAICompatibleProvider({
      baseUrl: "https://x",
      apiKey: "k",
      model: "m",
      stream: true,
      fetchFn: (async () => new Response(stream, { status: 200 })) as typeof fetch,
    });
    const out = await p.complete({ messages: [{ role: "user", content: "go" }], tools: [] });
    expect(out.toolCalls).toEqual([{ id: "call_9", name: "add", args: {} }]);
  });
});
