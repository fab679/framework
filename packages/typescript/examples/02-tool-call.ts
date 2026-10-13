import { z } from "zod";
import { Agent, MockModelProvider, ToolRegistry, defineTool } from "../src/index.js";

// Validated calls + artifact split: the model sees the snippet,
// downstream code keeps the document id.
const registry = new ToolRegistry();
registry.register(
  defineTool({
    name: "add",
    description: "Adds two numbers",
    schema: z.object({ a: z.number(), b: z.number() }),
    execute: async ({ a, b }) => ({ sum: a + b }),
  }),
);
registry.register(
  defineTool({
    name: "search",
    description: "Searches docs, keeps provenance out of context",
    schema: z.object({ query: z.string() }),
    execute: async ({ query }) => ({ snippet: `Result for "${query}"`, docId: "doc_123", page: 4 }),
    toResult: (out) => ({
      content: out.snippet,
      artifact: { document_id: out.docId, page: out.page },
    }),
  }),
);

console.log("tools:", registry.list().map((t) => t.name));
console.log("2 + 3 =", await registry.call("add", { a: 2, b: 3 }));

const agent = new Agent({ model: new MockModelProvider(), tools: registry.list() });
const res = await agent.run("add 2 and 3");
console.log("agent:", res.text);
console.log("artifacts:", res.toolCalls.map((c) => c.artifact ?? null));
