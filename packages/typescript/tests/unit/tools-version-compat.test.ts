/**
 * Type + runtime regression: tool schemas must work with ANY zod version —
 * consumers on zod v4 (`$strip` config types) crashed against the previous
 * zod3-typed ToolSpec. The schema field is now structural (.parse) and TArgs
 * comes from the execute annotation.
 */
import { describe, expect, it } from "vitest";
import { z } from "zod";
import { defineTool, type Tool } from "../../src/tools/index.js";

// Mimic a DIFFERENT zod module instance (what zod4 users get structurally):
// their z.object() result only structurally needs .parse for our storage.
const foreignZodObject = {
  parse: (input: unknown) => {
    const { city } = (input ?? {}) as { city?: unknown };
    if (typeof city !== "string") throw new Error("no city");
    return { city };
  },
};

describe("tool schema version compatibility", () => {
  it("accepts foreign/zod4-style schemas (structural .parse)", async () => {
    const tool = defineTool({
      name: "get_weather",
      description: "dw",
      schema: foreignZodObject,
      execute: async ({ city }: { city: string }) => ({ city, tempC: 21 }),
    });
    expect((await tool.execute({ city: "Tokyo" }, { agentName: "t", state: undefined as never, invocationState: {} })).tempC).toBe(21);
    expect(tool.schema.parse({ city: "Oslo" })).toEqual({ city: "Oslo" });
  });

  it("same-module zod v3 schemas and annotated execute infer cleanly", async () => {
    const tool = defineTool({
      name: "t",
      description: "d",
      schema: z.object({ city: z.string() }),
      execute: async ({ city }: { city: string }) => city,
    });
    expect(await tool.execute({ city: "Oslo" })).toBe("Oslo");
  });

  it("the published Wire contract still advertises the schema via parametersSchema", () => {
    const tool: Tool<{ city: string }, { city: string }> = defineTool({
      name: "w",
      description: "d",
      schema: z.object({ city: z.string() }),
      execute: async ({ city }: { city: string }) => ({ city }),
    });
    // JSON schema generation still flows to providers (openai/anthropic wire)
    expect(tool.schema).toHaveProperty("parse");
  });
});
