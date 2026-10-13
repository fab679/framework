import { describe, expect, it } from "vitest";
import { z } from "zod";
import { Agent } from "../../src/agent/index.js";
import { ToolRegistry, defineTool } from "../../src/tools/index.js";
import { InMemoryMemory } from "../../src/memory/index.js";
import { MockModelProvider } from "../../src/models/index.js";

describe("Agent", () => {
  it("returns mock text with no tools", async () => {
    const agent = new Agent({ model: new MockModelProvider() });
    const result = await agent.run("hello");
    expect(result.text).toBe("mock-response-1");
    expect(result.toolCalls).toHaveLength(0);
  });

  it("persists messages to memory", async () => {
    const memory = new InMemoryMemory();
    const agent = new Agent({ model: new MockModelProvider(), memory });
    await agent.run("hello");
    expect((await memory.load()).length).toBeGreaterThan(0);
  });
});

describe("ToolRegistry", () => {
  it("validates args with zod", async () => {
    const registry = new ToolRegistry();
    const echo = defineTool({
      name: "echo",
      description: "echoes",
      schema: z.object({ text: z.string() }),
      execute: async ({ text }) => ({ text }),
    });
    registry.register(echo);
    await expect(registry.call("echo", { text: "hi" })).resolves.toEqual({ text: "hi" });
    await expect(registry.call("echo", { text: 123 })).rejects.toThrow();
  });
});
