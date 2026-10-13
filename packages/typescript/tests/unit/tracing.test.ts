import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { z } from "zod";
import { Agent } from "../../src/agent/index.js";
import { defineTool } from "../../src/tools/index.js";
import {
  ConsoleSpanSink,
  JsonlSpanSink,
  MemorySpanSink,
  policyFilterMiddleware,
  tracingEvents,
} from "../../src/tracing/index.js";
import { scripted } from "./helpers.js";

describe("tracing exporter", () => {
  it("projects run lifecycle into OTel-shaped spans", async () => {
    const sink = new MemorySpanSink();
    const agent = new Agent({
      model: scripted(["ok"]),
      events: tracingEvents(sink, { traceId: "t" }),
    });
    await agent.run("q");
    const names = sink.spans.map((s) => s.name);
    expect(names).toContain("agent.step");
    expect(names).toContain("agent.done");
    expect(sink.spans[0].traceId).toBe("t");
    expect(sink.spans[0].spanId).toMatch(/^[0-9a-f]{16}$/);
  });

  it("links tool spans parent->child and writes JSONL", async () => {
    const dir = mkdtempSync(join(tmpdir(), "spans-"));
    const path = join(dir, "spans.jsonl");
    const sink = new JsonlSpanSink(path);
    const events = tracingEvents(sink);
    events.onToolCall?.({ tool: "t", args: {} });
    events.onToolResult?.({ tool: "t", output: 1 });
    events.onToken?.("x");
    events.onThinking?.("y");
    const lines = readFileSync(path, "utf8").trim().split("\n");
    expect(lines).toHaveLength(4);
    const call = JSON.parse(lines[0]) as { name: string; spanId: string };
    const result = JSON.parse(lines[1]) as { name: string; parentId: string };
    expect(call.name).toBe("tool.call");
    expect(result.parentId).toBe(call.spanId);
    const logs: string[] = [];
    const orig = console.log;
    console.log = (...a: unknown[]) => logs.push(a.map(String).join(" "));
    new ConsoleSpanSink().emit({ traceId: "t", spanId: "s", name: "n", startedAt: "x", attributes: {} });
    console.log = orig;
    expect(logs.join()).toContain("[trace]");
  });
});

describe("policy filter", () => {
  const t = (name: string) =>
    defineTool({ name, description: "d", schema: z.object({}), execute: async () => "ok" });

  it("denies with feedback by default, throws on error behavior", async () => {
    const agent = new Agent({
      model: {
        id: "s",
        complete: async () => ({
          text: "",
          toolCalls: [{ id: "c1", name: "evil", args: {} }],
          usage: { inputTokens: 1, outputTokens: 1 },
        }),
      },
      maxSteps: 1,
      tools: [t("evil")],
      middleware: [policyFilterMiddleware({ deny: [/evil/] })],
    });
    const res = await agent.run("go");
    expect(String(res.messages.find((m) => m.role === "tool")?.content)).toContain("Policy denies");

    const strict = new Agent({
      model: {
        id: "s",
        complete: async () => ({
          text: "",
          toolCalls: [{ id: "c1", name: "evil", args: {} }],
          usage: { inputTokens: 1, outputTokens: 1 },
        }),
      },
      maxSteps: 1,
      tools: [t("evil")],
      middleware: [policyFilterMiddleware({ deny: ["evil"], exitBehavior: "error" })],
    });
    await expect(strict.run("go")).rejects.toMatchObject({ code: "POLICY_DENY" });
  });

  it("allow-lists by name", async () => {
    const agent = new Agent({
      model: {
        id: "s",
        complete: async () => ({
          text: "done",
          toolCalls: [],
          usage: { inputTokens: 1, outputTokens: 1 },
        }),
      },
      tools: [t("good")],
      middleware: [policyFilterMiddleware({ allow: ["good"] })],
    });
    expect((await agent.run("go")).text).toBe("done");
  });
});
