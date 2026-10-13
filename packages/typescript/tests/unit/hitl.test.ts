import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { z } from "zod";
import { Agent } from "../../src/agent/index.js";
import { FileCheckpointer, withFileLock, type Checkpointer } from "../../src/checkpoint/index.js";
import { HitlInterrupt, hitlKey, humanInTheLoopMiddleware } from "../../src/middleware/index.js";
import { defineTool } from "../../src/tools/index.js";
import type { ModelCompletion } from "../../src/models/index.js";

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

const sendTool = () =>
  defineTool({
    name: "send",
    description: "send",
    schema: z.object({ to: z.string() }),
    execute: async ({ to }: { to: string }) => `sent to ${to}`,
  });

describe("HITL suspend/resume (local checkpointer)", () => {
  it("suspends, checkpoints, resumes with a decision", async () => {
    const dir = mkdtempSync(join(tmpdir(), "hitl-"));
    const checkpointer = new FileCheckpointer(dir);
    const mkAgent = () =>
      new Agent({
        model: toolStub([{ id: "c1", name: "send", args: { to: "a@x.com" } }], "finished"),
        tools: [sendTool()],
        middleware: [humanInTheLoopMiddleware({ interruptOn: { send: true } })],
      });

    const agent = mkAgent();
    const err = await agent.run("go", { checkpointer, threadId: "t1" }).then(
      () => undefined,
      (e: unknown) => e,
    );
    expect(err).toBeInstanceOf(HitlInterrupt);
    const interrupt = err as HitlInterrupt;
    expect(interrupt.tool).toBe("send");

    const cp = await checkpointer.load("t1");
    expect(cp?.pending).toHaveLength(1);
    expect(cp?.pending[0].interruptId).toBe(interrupt.interruptId);

    const agent2 = mkAgent();
    const res = await agent2.resume("t1", { [hitlKey("send", { to: "a@x.com" })]: "approve" }, { checkpointer });
    expect(res.text).toBe("finished");
    expect(res.toolCalls[0]?.output).toBe("sent to a@x.com");
    expect(interrupt.interruptId).toBe(hitlKey("send", { to: "a@x.com" }));
  });

  it("resume without checkpoint throws; suspend without checkpointer still throws interrupt", async () => {
    const dir = mkdtempSync(join(tmpdir(), "hitl-"));
    const agent = new Agent({
      model: toolStub([{ id: "c1", name: "send", args: { to: "a@x.com" } }]),
      tools: [sendTool()],
      middleware: [humanInTheLoopMiddleware({ interruptOn: { send: true } })],
    });
    await expect(agent.resume("ghost", {}, { checkpointer: new FileCheckpointer(dir) })).rejects.toMatchObject({
      code: "CHECKPOINT_MISSING",
    });
    await expect(agent.run("go")).rejects.toBeInstanceOf(HitlInterrupt);
  });
});

describe("withFileLock", () => {
  it("serializes holders and reclaims stale locks", async () => {
    const dir = mkdtempSync(join(tmpdir(), "locks-"));
    const lock = join(dir, "t.lock");
    const order: string[] = [];
    await withFileLock(lock, async () => {
      order.push("first");
      await expect(withFileLock(lock, async () => "x")).rejects.toThrow(/Lock held/);
    });
    await withFileLock(lock, async () => order.push("second"));
    expect(order).toEqual(["first", "second"]);

    writeFileSync(lock, "stale");
    const old = new Date(Date.now() - 60000);
    const { utimesSync } = await import("node:fs");
    utimesSync(lock, old, old);
    await withFileLock(lock, async () => order.push("reclaimed"), { staleMs: 1000 });
    expect(order).toContain("reclaimed");
  });
});

describe("HITL tool-name wildcard decisions", () => {
  it("a bare tool-name decision covers re-issued calls with changed args (live-model resume convergence)", async () => {
    const calls: string[] = [];
    // Round 1: model emits args A; round 2 (resume): model regenerates args B —
    // only a tool-scoped approval can converge.
    let n = 0;
    const model = {
      id: "stub",
      complete: async () => {
        n += 1;
        if (n === 1) {
          return { text: "", toolCalls: [{ id: "c1", name: "publish", args: { word: "old text" } }], usage: { inputTokens: 1, outputTokens: 1 } };
        }
        if (n === 2) {
          return { text: "", toolCalls: [{ id: "c2", name: "publish", args: { word: "regenerated different text" } }], usage: { inputTokens: 1, outputTokens: 1 } };
        }
        return { text: "finished", toolCalls: [], usage: { inputTokens: 1, outputTokens: 1 } };
      },
    };
    const publish = defineTool({
      name: "publish",
      description: "x",
      schema: z.object({ word: z.string() }),
      execute: async (a: { word: string }) => {
        calls.push(a.word);
        return "ok";
      },
    });
    const agent = () => new Agent({
      model,
      maxSteps: 2,
      tools: [publish],
      middleware: [humanInTheLoopMiddleware({ interruptOn: { publish: true } })],
    });
    const mem = new Map();
    const memCheckpointer: Checkpointer = {
      save: async (id, cp) => void mem.set(id, cp),
      load: async (id) => mem.get(id),
      delete: async (id) => void mem.delete(id),
    };
    try {
      await agent().run("go", { checkpointer: memCheckpointer, threadId: "t" });
      expect.unreachable("expected an interrupt");
    } catch (err) {
      expect(err).toBeInstanceOf(HitlInterrupt);
    }
    // Exact key for round 1 + TOOL-NAME wildcard for whatever the model re-issues.
    const resumed = await agent().resume("t", { publish: "approve" }, { checkpointer: memCheckpointer });
    expect(resumed.text).toBeTruthy();
    expect(calls).toEqual(["regenerated different text"]);
  });
});
