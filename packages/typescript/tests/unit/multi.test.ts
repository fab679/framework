import { describe, expect, it } from "vitest";
import { z } from "zod";
import { Agent } from "../../src/agent/index.js";
import { runOrchestrated } from "../../src/orchestration/index.js";
import { runWorkflow } from "../../src/workflows/index.js";
import { Swarm } from "../../src/swarm/index.js";
import { A2AClient, A2AServer, a2aAsTool } from "../../src/a2a/index.js";
import { defineTool } from "../../src/tools/index.js";
import type { ModelCompletion } from "../../src/models/index.js";

function textReply(text: string) {
  return {
    id: "t",
    complete: async (): Promise<ModelCompletion> => ({ text, toolCalls: [], usage: { inputTokens: 1, outputTokens: 1 } }),
  };
}

describe("shared invocationState", () => {
  it("reaches tools through orchestration", async () => {
    const seen: unknown[] = [];
    const agent = new Agent({
      model: {
        id: "s",
        complete: async (): Promise<ModelCompletion> => ({
          text: "",
          toolCalls: [{ id: "c1", name: "t", args: {} }],
          usage: { inputTokens: 1, outputTokens: 1 },
        }),
      },
      maxSteps: 1,
      tools: [
        defineTool({
          name: "t",
          description: "t",
          schema: z.object({}),
          execute: async (_a: unknown, ctx) => {
            seen.push(ctx?.invocationState.user_id);
            return "ok";
          },
        }),
      ],
    });
    await runOrchestrated({ strategy: "sequential", agents: [agent] }, "go", { invocationState: { user_id: "u9" } });
    expect(seen).toEqual(["u9"]);
  });

  it("reaches workflow nodes", async () => {
    const seen: unknown[] = [];
    await runWorkflow(
      [{ id: "a", run: async (_c, inv) => { seen.push(inv?.user_id); return 1; } }],
      { invocationState: { user_id: "w1" } },
    );
    expect(seen).toEqual(["w1"]);
  });
});

function handoffStub(plan: Record<number, { to?: string; message?: string; answer?: string }>) {
  let n = 0;
  return {
    id: "h",
    complete: async (): Promise<ModelCompletion> => {
      n += 1;
      const step = plan[n];
      if (step?.to) {
        return {
          text: "",
          toolCalls: [{ id: `h${n}`, name: "handoff_to_agent", args: { to: step.to, message: step.message ?? "", context: { from_step: n } } }],
          usage: { inputTokens: 1, outputTokens: 1 },
        };
      }
      return { text: step?.answer ?? "final", toolCalls: [], usage: { inputTokens: 1, outputTokens: 1 } };
    },
  };
}

function member(name: string, plan: Record<number, { to?: string; message?: string; answer?: string }>) {
  return new Agent({ name, description: `${name} specialist`, model: handoffStub(plan), maxSteps: 3 });
}

describe("Swarm", () => {
  it("hands off researcher -> coder and completes with shared context", async () => {
    const researcher = member("researcher", { 1: { to: "coder", message: "implement it" } });
    const coder = member("coder", { 1: { answer: "implemented" } });
    const swarm = new Swarm({ agents: [researcher, coder] });
    const res = await swarm.run("build a todo api");
    expect(res.status).toBe("COMPLETED");
    expect(res.text).toBe("implemented");
    expect(res.nodeHistory).toEqual(["researcher", "coder"]);
    expect(res.handoffs).toBe(1);
    expect(res.sharedContext.researcher).toMatchObject({ from_step: 1 });
  });

  it("supports entry points, iterations caps, and loop detection", async () => {
    const a = member("a", { 1: { to: "b", message: "yo" }, 2: { to: "b", message: "yo" } });
    const b = member("b", { 1: { to: "a", message: "back" }, 2: { to: "a", message: "back" } });
    const swarm = new Swarm({
      agents: [a, b],
      entryPoint: "b",
      maxHandoffs: 50,
      maxIterations: 50,
      loopDetectionWindow: 4,
      minUniqueAgents: 3,
    });
    const res = await swarm.run("ping pong");
    expect(res.status).toBe("FAILED");
    expect(res.error).toContain("Repetitive handoff");
  });

  it("streams node/handoff/result events", async () => {
    const researcher = member("researcher", { 1: { to: "coder", message: "go" } });
    const coder = member("coder", { 1: { answer: "done" } });
    const swarm = new Swarm({ agents: [researcher, coder] });
    const types: string[] = [];
    for await (const e of swarm.stream("task")) {
      types.push(e.type);
      if (e.type === "result") break;
    }
    expect(types).toContain("node_start");
    expect(types).toContain("handoff");
    expect(types).toContain("result");
  });

  it("rejects empty swarms and unknown entries", () => {
    expect(() => new Swarm({ agents: [] })).toThrow("Swarm needs at least one agent");
    expect(() => new Swarm({ agents: [member("a", {})], entryPoint: "ghost" })).toThrow("Unknown entry point");
  });
});

describe("A2A", () => {
  it("serves and consumes round-trip with context isolation", async () => {
    const server = new A2AServer({
      card: { name: "echo", description: "echoes" },
      agentFactory: () => new Agent({ name: "echo", model: textReply("hello-remote") }),
      maxContexts: 10,
      corsOrigins: ["https://app.example.com"],
    });
    const port = await server.start(0, "127.0.0.1");
    try {
      const client = new A2AClient(`http://127.0.0.1:${port}`);
      const card = await client.getCard();
      expect(card.name).toBe("echo");
      const r1 = await client.send("hi", "ctx-1");
      expect(r1.text).toBe("hello-remote");
      expect(r1.contextId).toBe("ctx-1");
      const tool = a2aAsTool(client, { name: "remote" });
      expect(await tool.execute({ input: "yo" })).toBe("hello-remote");
      // health + CORS preflight
      const ok = await fetch(`http://127.0.0.1:${port}/ok`);
      expect(ok.status).toBe(200);
      const pre = await fetch(`http://127.0.0.1:${port}/`, { method: "OPTIONS", headers: { origin: "https://app.example.com" } });
      expect(pre.headers.get("access-control-allow-origin")).toBe("https://app.example.com");
    } finally {
      await server.stop();
    }
  });

  it("surfaces transport errors", async () => {
    const client = new A2AClient("http://127.0.0.1:1", { timeoutMs: 500 });
    await expect(client.getCard()).rejects.toMatchObject({ code: "A2A_TRANSPORT" });
  });

  it("validates cards and surfaces RPC/HTTP errors", async () => {
    const json = (body: unknown, status = 200) =>
      (async () => new Response(JSON.stringify(body), { status })) as typeof fetch;
    await expect(new A2AClient("http://x", { fetchFn: json({}) }).getCard()).rejects.toMatchObject({ code: "A2A_CARD" });
    await expect(
      new A2AClient("http://x", { fetchFn: json({ error: { message: "boom", code: 1 } }) }).send("hi"),
    ).rejects.toMatchObject({ code: "A2A_ERROR" });
    await expect(new A2AClient("http://x", { fetchFn: json({}, 500) }).send("hi")).rejects.toMatchObject({
      code: "A2A_TRANSPORT",
    });
    const stringResult = new A2AClient("http://x", { fetchFn: json({ result: "plain" }) });
    expect((await stringResult.send("hi")).text).toBe("plain");
    const textField = new A2AClient("http://x", { fetchFn: json({ result: { text: "t" } }) });
    expect((await textField.send("hi")).text).toBe("t");
    const failing = new A2AClient("http://x", {
      fetchFn: (async () => { throw new Error("down"); }) as typeof fetch,
    });
    await expect(failing.send("hi")).rejects.toMatchObject({ code: "A2A_TRANSPORT" });
  });

  it("server handles unknown methods, 404s, errors, and evicts contexts", async () => {
    const server = new A2AServer({
      card: { name: "s" },
      agentFactory: (ctx) =>
        ctx === "bad"
          ? new Agent({ name: "s", model: { id: "x", complete: async () => { throw new Error("kaput"); } } })
          : new Agent({ name: "s", model: textReply("ok") }),
      maxContexts: 1,
    });
    const port = await server.start();
    const base = `http://127.0.0.1:${port}`;
    try {
      const rpc = async (body: unknown, path = "/") => {
        const r = await fetch(`${base}${path}`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
        return r.json() as Promise<Record<string, unknown>>;
      };
      expect(((await rpc({ jsonrpc: "2.0", id: 1, method: "nope", params: {} })).error as { code: number }).code).toBe(-32601);
      const notFound = await fetch(`${base}/missing`);
      expect(notFound.status).toBe(404);
      await rpc({ jsonrpc: "2.0", id: 2, method: "message/send", params: { message: { parts: [{ text: "a" }] }, contextId: "c1" } });
      await rpc({ jsonrpc: "2.0", id: 3, method: "message/send", params: { message: { parts: [{ text: "b" }] }, contextId: "c2" } });
      const anon = (await rpc({ jsonrpc: "2.0", id: 4, method: "message/send", params: { message: { parts: [{ text: "c" }] } } })) as { result: { contextId: string } };
      expect(anon.result.contextId).toMatch(/^ctx-/);
      const failed = (await rpc({ jsonrpc: "2.0", id: 5, method: "message/send", params: { message: { parts: [{ text: "d" }] }, contextId: "bad" } })) as { error: { message: string } };
      expect(failed.error.message).toContain("kaput");
    } finally {
      await server.stop();
    }
  });
});
