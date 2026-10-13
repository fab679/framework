import { describe, expect, it } from "vitest";
import { z } from "zod";
import { Agent } from "../../src/agent/index.js";
import { collectStream } from "../../src/streaming/index.js";
import { modelCallLimit, toolRetry } from "../../src/middleware/index.js";
import { defineTool } from "../../src/tools/index.js";
import type { CompleteOptions } from "../../src/models/index.js";

function scripted(responses: { text?: string; thinking?: string; toolCalls?: { id: string; name: string; args: unknown }[] }[]) {
  let n = 0;
  return {
    id: "scripted",
    complete: async (opts: CompleteOptions) => {
      const r = responses[Math.min(n++, responses.length - 1)];
      opts.onThinking?.(r.thinking ?? "");
      opts.onToken?.(r.text ?? "");
      return { text: r.text ?? "", thinking: r.thinking, toolCalls: r.toolCalls ?? [], usage: { inputTokens: 1, outputTokens: 1 } };
    },
  };
}

describe("agent.stream()", () => {
  it("yields step/token/thinking/tool/done in order", async () => {
    const agent = new Agent({
      model: scripted([
        { thinking: "need calc", toolCalls: [{ id: "c1", name: "add", args: { a: 1, b: 2 } }] },
        { text: "3" },
      ]),
      tools: [defineTool({ name: "add", description: "add", schema: z.object({ a: z.number(), b: z.number() }), execute: async ({ a, b }) => a + b })],
    });
    const { events, result } = await collectStream(agent.stream("1+2?"));
    expect(events.map((e) => e.type)).toEqual(["step", "thinking", "tool_call", "tool_result", "step", "token", "done"]);
    expect(result.text).toBe("3");
  });

  it("run() and stream() agree", async () => {
    const mk = () => new Agent({ model: scripted([{ text: "hi" }]) });
    const viaRun = await mk().run("q");
    const { result } = await collectStream(mk().stream("q"));
    expect(result.text).toBe(viaRun.text);
  });
});

describe("middleware", () => {
  it("modelCallLimit caps model calls", async () => {
    const agent = new Agent({ model: scripted([{ text: "a" }]), middleware: [modelCallLimit(0)] });
    await expect(agent.run("q")).rejects.toMatchObject({ code: "BUDGET_EXCEEDED" });
  });

  it("toolRetry recovers flaky tools", async () => {
    let n = 0;
    const agent = new Agent({
      model: scripted([
        { toolCalls: [{ id: "c1", name: "flaky", args: {} }] },
        { text: "recovered" },
      ]),
      tools: [defineTool({ name: "flaky", description: "f", schema: z.object({}), execute: async () => { n += 1; if (n < 2) throw new Error("boom"); return "ok"; } })],
      middleware: [toolRetry({ attempts: 3 })],
    });
    const res = await agent.run("go");
    expect(res.text).toBe("recovered");
    expect(n).toBe(2);
  });

  it("beforeTool rewrites calls, afterTool rewrites outputs", async () => {
    const seen: unknown[] = [];
    const agent = new Agent({
      model: scripted([
        { toolCalls: [{ id: "c1", name: "echo", args: { v: "x" } }] },
        { text: "done" },
      ]),
      tools: [defineTool({ name: "echo", description: "e", schema: z.object({ v: z.string() }), execute: async ({ v }) => { seen.push(v); return v; } })],
      middleware: [
        {
          name: "rewrite",
          beforeTool: (call) => ({ ...call, args: { v: "rewritten" } }),
          afterTool: (_call, output) => `wrapped:${String(output)}`,
        },
      ],
    });
    const res = await agent.run("go");
    expect(seen).toEqual(["rewritten"]);
    expect(res.toolCalls[0].output).toBe("wrapped:rewritten");
  });

  it("beforeModel can inject context", async () => {
    let sawSystem = "";
    const agent = new Agent({
      model: {
        id: "spy",
        complete: async (opts) => {
          sawSystem = String(opts.messages[0]?.content ?? "");
          return { text: "ok", toolCalls: [], usage: { inputTokens: 1, outputTokens: 1 } };
        },
      },
      middleware: [{ name: "inject", beforeModel: (req) => ({ ...req, messages: [{ role: "system", content: "CTX " }, ...req.messages.slice(1)] }) }],
    });
    await agent.run("q");
    expect(sawSystem).toBe("CTX ");
  });
});
