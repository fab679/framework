import { z } from "zod";
import {
  Agent,
  humanInTheLoopMiddleware,
  piiMiddleware,
  toolCallLimitMiddleware,
  toolRetryMiddleware,
} from "../src/index.js";

// Offline demo: guardrails stack — approval gate + PII scrub + budgets.
// Decisions are stubbed (wire `decide` to a CLI prompt or API in production).
const decisions: Record<string, "approve" | "edit" | "reject"> = { send_email: "approve" };

const agent = new Agent({
  model: {
    id: "stub",
    complete: async (opts) => {
      const last = opts.messages.at(-1);
      if (String(last?.content ?? "").includes("send")) {
        return {
          text: "",
          toolCalls: [{ id: "c1", name: "send_email", args: { to: "boss@corp.com", body: "hi ada@example.com" } }],
          usage: { inputTokens: 1, outputTokens: 1 },
        };
      }
      return { text: "done", toolCalls: [], usage: { inputTokens: 1, outputTokens: 1 } };
    },
  },
  tools: [
    {
      name: "send_email",
      description: "Sends an email",
      schema: z.object({ to: z.string(), body: z.string() }),
      execute: async ({ to }: { to: string }) => `sent to ${to}`,
    },
  ],
  middleware: [
    humanInTheLoopMiddleware({
      interruptOn: { send_email: { allowedDecisions: ["approve", "edit", "reject"] } },
      decide: ({ name, args }) => {
        console.log(`APPROVAL needed: ${name}`, args);
        return decisions[name] === "reject" ? { reject: "Declined by human." } : "approve";
      },
    }),
    piiMiddleware("email", { strategy: "redact" }),
    toolCallLimitMiddleware({ toolName: "send_email", runLimit: 3 }),
    toolRetryMiddleware({ maxRetries: 2, initialDelayMs: 1 }),
  ],
});

const res = await agent.run("send a greeting to the boss (mention ada@example.com)");
console.log("FINAL:", res.text);
console.log("TOOL OUTPUT:", res.toolCalls[0]?.output);
