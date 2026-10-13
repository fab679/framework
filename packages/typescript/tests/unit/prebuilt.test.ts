import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { z } from "zod";
import { Agent } from "../../src/agent/index.js";
import {
  ClearToolUsesEdit,
  contextEditingMiddleware,
  createMiddleware,
  createSubAgentMiddleware,
  filesystemMiddleware,
  getRetryable,
  humanInTheLoopMiddleware,
  llmToolSelectorMiddleware,
  modelFallbackMiddleware,
  modelRetryMiddleware,
  piiMiddleware,
  stampRetryable,
  summarizationMiddleware,
  todoListMiddleware,
  toolCallLimitMiddleware,
  toolEmulatorMiddleware,
  toolRetryMiddleware,
} from "../../src/middleware/index.js";
import { initChatModel } from "../../src/models/factory.js";
import { defineTool } from "../../src/tools/index.js";
import type { CompleteOptions, ModelCompletion } from "../../src/models/index.js";

function scripted(texts: string[]) {
  let n = 0;
  return {
    id: "scripted",
    complete: async (): Promise<ModelCompletion> => ({
      text: texts[Math.min(n++, texts.length - 1)],
      toolCalls: [],
      usage: { inputTokens: 1, outputTokens: 1 },
    }),
  };
}

function toolStub(calls: { id: string; name: string; args: unknown }[], finalText = "done") {
  let n = 0;
  return {
    id: "stub",
    complete: async (): Promise<ModelCompletion> => {
      if (n < calls.length) {
        const c = calls[n++];
        return { text: "", toolCalls: [{ ...c }], usage: { inputTokens: 1, outputTokens: 1 } };
      }
      return { text: finalText, toolCalls: [], usage: { inputTokens: 1, outputTokens: 1 } };
    },
  };
}

describe("retry verdicts", () => {
  it("stamp/get round-trip; AgentError maps to its flag", () => {
    const e = stampRetryable(new Error("x"), true);
    expect(getRetryable(e)).toBe(true);
    expect(e instanceof Error).toBe(true);
    expect(getRetryable(new Error("y"))).toBeUndefined();
    expect(getRetryable("nope")).toBeUndefined();
  });
});

describe("modelRetryMiddleware", () => {
  it("retries transient failures with backoff", async () => {
    let n = 0;
    const flaky = {
      id: "flaky",
      complete: async (): Promise<ModelCompletion> => {
        n += 1;
        if (n < 3) throw stampRetryable(new Error("down"), true);
        return { text: "ok", toolCalls: [], usage: { inputTokens: 1, outputTokens: 1 } };
      },
    };
    const agent = new Agent({ model: flaky, middleware: [modelRetryMiddleware({ maxRetries: 3, initialDelayMs: 1 })] });
    expect((await agent.run("q")).text).toBe("ok");
    expect(n).toBe(3);
  });

  it("honors fatal verdicts immediately", async () => {
    let n = 0;
    const fatal = {
      id: "fatal",
      complete: async (): Promise<ModelCompletion> => {
        n += 1;
        throw stampRetryable(new Error("auth"), false);
      },
    };
    const agent = new Agent({ model: fatal, middleware: [modelRetryMiddleware({ maxRetries: 3, initialDelayMs: 1 })] });
    await expect(agent.run("q")).rejects.toThrow("auth");
    expect(n).toBe(1);
  });

  it("onFailure continue ends gracefully", async () => {
    const down = { id: "down", complete: async (): Promise<ModelCompletion> => { throw new Error("down"); } };
    const agent = new Agent({
      model: down,
      middleware: [modelRetryMiddleware({ maxRetries: 0, initialDelayMs: 1, onFailure: "continue" })],
    });
    const res = await agent.run("q");
    expect(res.text).toContain("down");
  });
});

describe("modelFallbackMiddleware", () => {
  it("falls to the next working provider", async () => {
    const primary = { id: "p", complete: async (): Promise<ModelCompletion> => { throw new Error("outage"); } };
    const fallback = scripted(["fallback-ok"]);
    const agent = new Agent({ model: primary, middleware: [modelFallbackMiddleware(fallback)] });
    expect((await agent.run("q")).text).toBe("fallback-ok");
  });
});

describe("toolCallLimitMiddleware", () => {
  it("blocks past runLimit with feedback by default", async () => {
    const agent = new Agent({
      model: toolStub([{ id: "c1", name: "t", args: {} }, { id: "c2", name: "t", args: {} }], "fin"),
      tools: [defineTool({ name: "t", description: "t", schema: z.object({}), execute: async () => "v" })],
      middleware: [toolCallLimitMiddleware({ toolName: "t", runLimit: 1 })],
    });
    const res = await agent.run("go");
    expect(res.text).toBe("fin");
    expect(res.messages.some((m) => m.role === "tool" && String(m.content).includes("limit exceeded"))).toBe(true);
  });

  it("error behavior throws", async () => {
    const agent = new Agent({
      model: toolStub([{ id: "c1", name: "t", args: {} }]),
      tools: [defineTool({ name: "t", description: "t", schema: z.object({}), execute: async () => "v" })],
      middleware: [toolCallLimitMiddleware({ runLimit: 0, exitBehavior: "error" })],
    });
    await expect(agent.run("go")).rejects.toMatchObject({ code: "BUDGET_EXCEEDED" });
  });
});

describe("humanInTheLoopMiddleware", () => {
  const t = () => defineTool({ name: "send", description: "send", schema: z.object({ to: z.string() }), execute: async ({ to }: { to: string }) => `sent to ${to}` });

  it("approve passes through", async () => {
    const agent = new Agent({
      model: toolStub([{ id: "c1", name: "send", args: { to: "a@x.com" } }]),
      tools: [t()],
      middleware: [humanInTheLoopMiddleware({ interruptOn: { send: true }, decide: () => "approve" })],
    });
    const res = await agent.run("go");
    expect(res.toolCalls[0].output).toBe("sent to a@x.com");
  });

  it("edit rewrites args", async () => {
    const agent = new Agent({
      model: toolStub([{ id: "c1", name: "send", args: { to: "wrong@x.com" } }]),
      tools: [t()],
      middleware: [humanInTheLoopMiddleware({ interruptOn: { send: true }, decide: () => ({ edit: { to: "right@x.com" } }) })],
    });
    const res = await agent.run("go");
    expect(res.toolCalls[0].output).toBe("sent to right@x.com");
  });

  it("reject feeds back without executing", async () => {
    let executed = false;
    const agent = new Agent({
      model: toolStub([{ id: "c1", name: "send", args: { to: "a@x.com" } }], "understood"),
      tools: [defineTool({ name: "send", description: "s", schema: z.object({ to: z.string() }), execute: async () => { executed = true; return "x"; } })],
      middleware: [humanInTheLoopMiddleware({ interruptOn: { send: true }, decide: () => ({ reject: "No sending today." }) })],
    });
    const res = await agent.run("go");
    expect(executed).toBe(false);
    expect(res.messages.some((m) => m.role === "tool" && String(m.content).includes("No sending today"))).toBe(true);
  });

  it("unmatched tools skip approval", async () => {
    const agent = new Agent({
      model: toolStub([{ id: "c1", name: "send", args: { to: "a@x.com" } }]),
      tools: [t()],
      middleware: [humanInTheLoopMiddleware({ interruptOn: { other: true }, decide: () => ({ reject: "x" }) })],
    });
    expect((await agent.run("go")).toolCalls).toHaveLength(1);
  });
});

describe("piiMiddleware", () => {
  it("redacts emails from input", async () => {
    let seen = "";
    const agent = new Agent({
      model: { id: "spy", complete: async (o: CompleteOptions) => { seen = String(o.messages.at(-1)?.content); return { text: "ok", toolCalls: [], usage: { inputTokens: 1, outputTokens: 1 } }; } },
      middleware: [piiMiddleware("email")],
    });
    await agent.run("contact ada@example.com please");
    expect(seen).toContain("[REDACTED_EMAIL]");
    expect(seen).not.toContain("ada@example.com");
  });

  it("mask/hash/block strategies", async () => {
    const agent = (strategy: "mask" | "hash" | "block") =>
      new Agent({ model: scripted(["ok"]), middleware: [piiMiddleware("email", { strategy })] });
    await agent("mask").run("a@b.com");
    await agent("hash").run("a@b.com");
    await expect(agent("block").run("a@b.com")).rejects.toMatchObject({ code: "PII_BLOCKED" });
  });

  it("custom regex detector + tool-result scrubbing", async () => {
    const agent = new Agent({
      model: toolStub([{ id: "c1", name: "leak", args: {} }], "done"),
      tools: [defineTool({ name: "leak", description: "l", schema: z.object({}), execute: async () => "key sk-abc123" })],
      middleware: [piiMiddleware("api_key", { detector: "sk-[a-z0-9]+", strategy: "redact", applyToToolResults: true })],
    });
    const res = await agent.run("go");
    expect(String(res.messages.find((m) => m.role === "tool")?.content)).toContain("[REDACTED_API_KEY]");
  });
});

describe("summarizationMiddleware", () => {
  it("compresses history past the token trigger", async () => {
    const seen: number[] = [];
    const agent = new Agent({
      model: {
        id: "spy",
        complete: async (o: CompleteOptions) => {
          seen.push(o.messages.length);
          return { text: "ok", toolCalls: [], usage: { inputTokens: 1, outputTokens: 1 } };
        },
      },
      middleware: [summarizationMiddleware({ model: scripted(["short version"]), triggerTokens: 10, keepMessages: 1 })],
    });
    await agent.run("word ".repeat(100));
    expect(seen[0]).toBeLessThan(4);
  });
});

describe("contextEditingMiddleware", () => {
  it("clears old tool outputs past budget, keeps newest", async () => {
    const edit = new ClearToolUsesEdit({ triggerTokens: 5, keep: 1, placeholder: "[cleared]" });
    const mw = contextEditingMiddleware({ edits: [edit] });
    const req = {
      messages: [
        { role: "user" as const, content: "q" },
        { role: "tool" as const, content: "old-1", toolCallId: "1", toolName: "t" },
        { role: "tool" as const, content: "old-2", toolCallId: "2", toolName: "t" },
        { role: "tool" as const, content: "new-3", toolCallId: "3", toolName: "t" },
      ],
      tools: [],
    };
    const out = await mw.beforeModel!(req, { step: 0 });
    const contents = out?.messages.filter((m) => m.role === "tool").map((m) => m.content);
    expect(contents).toEqual(["[cleared]", "[cleared]", "new-3"]);
  });
});

describe("todoListMiddleware", () => {
  it("provides write_todos and tracks state", async () => {
    const mw = todoListMiddleware();
    const agent = new Agent({
      model: toolStub([{ id: "c1", name: "write_todos", args: { todos: [{ content: "a", status: "active" }] } }], "planned"),
      tools: [],
      middleware: [mw],
    });
    await agent.run("plan it");
    expect(mw.listTodos()).toEqual([{ content: "a", status: "active" }]);
  });
});

describe("llmToolSelectorMiddleware", () => {
  it("narrows tools to the selector's picks", async () => {
    let offered: string[] = [];
    const agent = new Agent({
      model: {
        id: "main",
        complete: async (o: CompleteOptions) => {
          offered = (o.tools ?? []).map((t) => t.name);
          return { text: "ok", toolCalls: [], usage: { inputTokens: 1, outputTokens: 1 } };
        },
      },
      tools: [
        defineTool({ name: "alpha", description: "a", schema: z.object({}), execute: async () => 1 }),
        defineTool({ name: "beta", description: "b", schema: z.object({}), execute: async () => 2 }),
      ],
      middleware: [
        llmToolSelectorMiddleware({
          model: { id: "sel", complete: async () => ({ text: "beta", toolCalls: [], usage: { inputTokens: 1, outputTokens: 1 } }) },
        }),
      ],
    });
    await agent.run("use beta");
    expect(offered).toEqual(["beta"]);
  });
});

describe("toolEmulatorMiddleware", () => {
  it("emulates without executing", async () => {
    let executed = false;
    const agent = new Agent({
      model: toolStub([{ id: "c1", name: "real", args: {} }], "done"),
      tools: [defineTool({ name: "real", description: "r", schema: z.object({}), execute: async () => { executed = true; return "x"; } })],
      middleware: [toolEmulatorMiddleware({ model: scripted(["emulated-output"]) })],
    });
    const res = await agent.run("go");
    expect(executed).toBe(false);
    expect(res.toolCalls[0].output).toBe("emulated-output");
  });
});

describe("filesystemMiddleware", () => {
  it("round-trips files inside root, rejects escapes", async () => {
    const root = mkdtempSync(join(tmpdir(), "fs-"));
    writeFileSync(join(root, "a.txt"), "hello");
    const agent = new Agent({
      model: toolStub([
        { id: "c1", name: "read_file", args: { path: "a.txt" } },
        { id: "c2", name: "write_file", args: { path: "b.txt", content: "hi" } },
      ], "done"),
      tools: [],
      middleware: [filesystemMiddleware({ root })],
    });
    const res = await agent.run("go");
    expect(res.toolCalls[0].output).toBe("hello");
    expect(res.toolCalls[1].output).toContain("b.txt");
    const evil = new Agent({
      model: toolStub([{ id: "c1", name: "read_file", args: { path: "../evil" } }]),
      tools: [],
      middleware: [filesystemMiddleware({ root })],
    });
    await expect(evil.run("go")).rejects.toMatchObject({ code: "FS_ESCAPE" });
  });
});

describe("createSubAgentMiddleware", () => {
  it("delegates through the task tool", async () => {
    const agent = new Agent({
      model: toolStub([{ id: "c1", name: "task", args: { subagent: "wx", input: "weather?" } }], "done"),
      tools: [],
      middleware: [
        createSubAgentMiddleware({
          model: scripted(["sunny"]),
          subagents: [{ name: "wx", description: "weather", tools: [] }],
        }),
      ],
    });
    const res = await agent.run("go");
    expect(res.toolCalls[0].output).toBe("sunny");
  });

  it("rejects unknown subagents", async () => {
    const agent = new Agent({
      model: toolStub([{ id: "c1", name: "task", args: { subagent: "ghost", input: "x" } }]),
      tools: [],
      middleware: [createSubAgentMiddleware({ model: scripted(["s"]), subagents: [] })],
    });
    await expect(agent.run("go")).rejects.toMatchObject({ code: "SUBAGENT_UNKNOWN" });
  });
});

describe("toolRetryMiddleware full", () => {
  it("backoff + filter + onFailure formatter", async () => {
    let n = 0;
    const agent = new Agent({
      model: toolStub([{ id: "c1", name: "db", args: {} }], "done"),
      tools: [defineTool({ name: "db", description: "d", schema: z.object({}), execute: async (): Promise<string> => { n += 1; throw stampRetryable(new Error("busy"), true); } })],
      middleware: [toolRetryMiddleware({ maxRetries: 2, initialDelayMs: 1, onFailure: (e) => `DB down: ${e.message}` })],
    });
    const res = await agent.run("go");
    expect(n).toBe(3);
    expect(String(res.messages.find((m) => m.role === "tool")?.content)).toContain("DB down");
  });
});

describe("createMiddleware + initChatModel", () => {
  it("identity factory preserves hooks", () => {
    const m = createMiddleware({ name: "x", beforeModel: (r) => r });
    expect(m.name).toBe("x");
  });

  it("resolves provider:model specs", () => {
    const t = initChatModel("together:Qwen/Qwen3.5-9B", { apiKey: "k" });
    expect(t.id).toContain("Qwen");
    expect(() => initChatModel("nope:Model", { apiKey: "k" })).toThrow(/unknown provider/);
    expect(() => initChatModel("openai:gpt-x")).toThrow(/OPENAI_API_KEY/);
  });
});
