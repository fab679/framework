import { z } from "zod";
import { Agent, InMemoryMemory, MockModelProvider, defineTool, consoleEvents } from "../src/index.js";

const getTime = defineTool({
  name: "get_time",
  description: "Returns current ISO time",
  schema: z.object({}),
  execute: async () => ({ time: new Date().toISOString() }),
});

const agent = new Agent({
  name: "demo",
  model: new MockModelProvider(),
  tools: [getTime],
  memory: new InMemoryMemory(),
  events: consoleEvents(),
});

const result = await agent.run("What time is it?");
console.log("FINAL:", result.text);
console.log("USAGE:", result.usage);
