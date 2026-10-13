import { describe, expect, it } from "vitest";
import { z } from "zod";
import { Agent } from "../../src/agent/index.js";
import { AgentState } from "../../src/state/index.js";
import { defineTool } from "../../src/tools/index.js";
import type { ModelCompletion } from "../../src/models/index.js";

function textReply(text: string) {
  return {
    id: "t",
    complete: async (): Promise<ModelCompletion> => ({ text, toolCalls: [], usage: { inputTokens: 1, outputTokens: 1 } }),
  };
}

describe("AgentState", () => {
  it("validates JSON-serializability", () => {
    const s = new AgentState({ a: 1 });
    expect(s.get("a")).toBe(1);
    s.set("b", { nested: [1, 2] });
    expect(() => s.set("fn", () => 1)).toThrow(/JSON-serializable/);
    s.delete("a");
    expect(s.get("a")).toBeUndefined();
    expect(s.get()).toEqual({ b: { nested: [1, 2] } });
    expect(AgentState.fromJSON(s.toJSON()).get()).toEqual(s.get());
  });
});

describe("ToolContext + invocationState", () => {
  it("tools read state + invocation, mutations visible, result carries state", async () => {
    const seen: unknown[] = [];
    const agent = new Agent({
      model: {
        id: "s",
        complete: async (): Promise<ModelCompletion> => ({
          text: "",
          toolCalls: [{ id: "c1", name: "track", args: { a: "login" } }],
          usage: { inputTokens: 1, outputTokens: 1 },
        }),
      },
      state: { count: 0 },
      tools: [
        defineTool({
          name: "track",
          description: "t",
          schema: z.object({ a: z.string() }),
          execute: async ({ a }: { a: string }, ctx) => {
            seen.push([ctx?.agentName, ctx?.invocationState.user_id]);
            if (ctx?.invocationState) (ctx.invocationState.calls as string[]).push(a);
            ctx?.state.set("count", ((ctx?.state.get("count") as number) ?? 0) + 1);
            return "ok";
          },
        }),
      ],
      maxSteps: 1,
    });
    const invocationState: Record<string, unknown> = { user_id: "u-1", calls: [] as string[] };
    const res = await agent.run("go", { invocationState });
    expect(seen).toEqual([["agent", "u-1"]]);
    expect(invocationState.calls).toEqual(["login"]);
    expect(agent.state.get("count")).toBe(1);
    expect(res.invocationState).toBe(invocationState);
  });
});

describe("agent.messages + initialMessages", () => {
  it("persists history between calls and seeds initials", async () => {
    const agent = new Agent({
      model: textReply("hi"),
      initialMessages: [
        { role: "user", content: "I am Ada" },
        { role: "assistant", content: "noted" },
      ],
    });
    expect(agent.messages).toEqual([]);
    await agent.run("hello");
    const msgs = agent.messages;
    expect(msgs.filter((m) => m.role === "user").map((m) => m.content)).toEqual(["I am Ada", "hello"]);
    await agent.run("again");
    expect(agent.messages.length).toBeGreaterThan(msgs.length);
  });
});
