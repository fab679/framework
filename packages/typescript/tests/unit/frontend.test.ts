import { describe, expect, it } from "vitest";
import { Agent } from "../../src/agent/index.js";
import { AgentClient, handleAgentRoute } from "../../src/client/index.js";
import { A2AServer } from "../../src/a2a/index.js";
import type { ModelCompletion } from "../../src/models/index.js";

function textReply(text: string) {
  return {
    id: "t",
    complete: async (opts: { onToken?: (d: string) => void }): Promise<ModelCompletion> => {
      opts.onToken?.(text);
      return { text, toolCalls: [], usage: { inputTokens: 1, outputTokens: 1 } };
    },
  };
}

describe("AgentClient", () => {
  it("sends + streams SSE events", async () => {
    const server = new A2AServer({
      card: { name: "s" },
      agentFactory: () => new Agent({ name: "s", model: textReply("hello-stream") }),
    });
    const port = await server.start();
    try {
      const client = new AgentClient(`http://127.0.0.1:${port}`);
      expect((await client.send("hi")).text).toBe("hello-stream");
      const types: string[] = [];
      for await (const e of client.stream("hi")) types.push(e.type);
      expect(types).toContain("token");
      expect(types.at(-1)).toBe("done");
    } finally {
      await server.stop();
    }
  });

  it("handleAgentRoute bridges Next-style requests to SSE", async () => {
    const server = new A2AServer({
      card: { name: "s" },
      agentFactory: () => new Agent({ name: "s", model: textReply("routed") }),
    });
    const port = await server.start();
    try {
      const client = new AgentClient(`http://127.0.0.1:${port}`);
      const res = await handleAgentRoute(
        new Request("http://x/api/agent", { method: "POST", body: JSON.stringify({ text: "hi" }) }),
        client,
      );
      expect(res.headers.get("content-type")).toContain("text/event-stream");
      const body = await res.text();
      expect(body).toContain("routed");
      expect(body).toContain("[DONE]");
    } finally {
      await server.stop();
    }
  });
});

describe("pause/branch/timeline", () => {
  it("pauses and resumes without interrupts", async () => {
    const { FileCheckpointer } = await import("../../src/checkpoint/index.js");
    const { mkdtempSync } = await import("node:fs");
    const { tmpdir } = await import("node:os");
    const { join } = await import("node:path");
    const checkpointer = new FileCheckpointer(mkdtempSync(join(tmpdir(), "pause-")));
    const agent = new Agent({ model: textReply("one") });
    await agent.run("first");
    const paused = await agent.pause("thread-1", { checkpointer });
    expect(paused.messages).toBeGreaterThan(0);
    const agent2 = new Agent({ model: textReply("two") });
    const res = await agent2.resume("thread-1", {}, { checkpointer });
    expect(res.text).toBe("two");
    expect(agent2.messages.some((m) => m.content === "first")).toBe(true);
  });

  it("edits messages and branches with timeline", async () => {
    const agent = new Agent({ model: textReply("ok") });
    await agent.run("hello");
    const target = agent.messages.find((m) => m.role === "user" && m.content === "hello")!;
    const updated = await agent.editMessage(target.id!, "hello edited");
    expect(updated.content).toBe("hello edited");
    expect(agent.messages.find((m) => m.id === target.id)?.content).toBe("hello edited");
    await expect(agent.editMessage("missing-id", "x")).rejects.toMatchObject({ code: "MESSAGE_NOT_FOUND" });

    const child = await agent.branch("try-b");
    expect(child.messages.length).toBe(agent.messages.length);
    expect(child.state).toBeDefined();
    await child.run("child turn");
    expect(agent.messages.length).toBeLessThan(child.messages.length);

    const kinds = agent.timeline.map((t) => t.kind);
    expect(kinds).toContain("run");
    expect(kinds).toContain("edit");
    expect(kinds).toContain("branch");
    expect(child.timeline.map((t) => t.kind)).toContain("branch");
  });
});
