import { z } from "zod";
import { Agent, toolStrategy } from "../src/index.js";

// Offline demo: tool-strategy structured output. The stub model returns
// garbage first (watch it self-repair), then a valid contact.
const Contact = z.object({ name: z.string(), email: z.string() });

const returns = [
  { name: "structured_response", args: { name: "Ada" } }, // invalid: no email
  { name: "structured_response", args: { name: "Ada", email: "ada@ex.com" } },
];
let n = 0;
const stub = {
  id: "stub",
  complete: async () => {
    const c = returns[Math.min(n++, returns.length - 1)];
    return { text: "", toolCalls: [{ id: `call_${n}`, ...c }], usage: { inputTokens: 1, outputTokens: 1 } };
  },
};

const agent = new Agent({ model: stub, responseFormat: toolStrategy(Contact) });
const res = await agent.run("Extract the contact: Ada, ada@ex.com");
console.log("STRUCTURED:", res.structuredResponse);
