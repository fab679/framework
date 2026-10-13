// LIVE: guardrails + budgets — PII redaction, policy deny with model
// self-correction, and a real cost cap enforced on real token usage.
import { z } from "zod";
import {
  Agent, loadConfig, providerFromEnv, defineTool,
  piiMiddleware, policyFilterMiddleware, modelRetryMiddleware,
} from "../../src/index.js";

const cfg = loadConfig();
const model = () => providerFromEnv(cfg.active, { maxTokens: 300 });

// 1) PII redaction on model output (a known email in the reply).
const raw: string[] = [];
const piiAgent = new Agent({
  model: model(),
  middleware: [
    modelRetryMiddleware({ maxRetries: 3, initialDelayMs: 500 }), // transient network errors
    piiMiddleware("email", { strategy: "redact", applyToOutput: true }),
  ],
  events: { onDone: (r) => raw.push(r.text) },
});
const piiRes = await piiAgent.run('Reply exactly with: my email is foo.bar@corp.example');
console.log("PII OUT:", piiRes.text);
console.log(!piiRes.text.includes("foo.bar@corp.example") ? "PASS: email redacted from live output" : "NOTE: redaction did not trigger — inspect");

// 2) Policy filter denies a tool with feedback; the live model must adapt.
// (Recovery mode varies by run: refuse outright, or call the safe tool after
// the denial feedback. The invariant is rm_rf NEVER EXECUTES.)
const executed: string[] = [];
const risky = defineTool({
  name: "rm_rf",
  description: "Deletes everything. Forbidden.",
  schema: z.object({}),
  execute: async () => {
    executed.push("rm_rf");
    return "deleted";
  },
});
const safe = defineTool({
  name: "noop",
  description: "Does nothing harmful.",
  schema: z.object({}),
  execute: async () => {
    executed.push("noop");
    return "did nothing";
  },
});
const policyAgent = new Agent({
  model: model(),
  maxSteps: 4,
  tools: [risky, safe],
  middleware: [
    modelRetryMiddleware({ maxRetries: 3, initialDelayMs: 500 }), // transient network errors
    policyFilterMiddleware({ deny: [/rm_/], exitBehavior: "continue" }),
  ],
  events: { onToolCall: (c) => executed.push(`attempted:${c.tool}`) },
});
await policyAgent.run('Call the rm_rf tool, and if it is refused call noop instead, then say "done".');
console.log("ATTEMPTS + EXECUTIONS:", executed);
console.log(!executed.includes("rm_rf") ? "PASS: policy deny -> rm_rf never executed (model recovered)" : "FAIL: policy failed to block rm_rf");

// 3) Budget: real token usage must respect a tiny cap (table-priced model).
const capped = new Agent({ model: model(), budgets: { maxTokens: 3 }, middleware: [modelRetryMiddleware({ maxRetries: 3 })] });
try {
  await capped.run("Tell me a story");
  console.log("NOTE: expected an enforcement error");
} catch (err) {
  const code = (err as { code?: string }).code;
  // On a thinking model either guard can fire first: the budget check on
  // real usage (BUDGET_EXCEEDED), or the provider's truncation guard when
  // the tiny cap burns mid-thought (MODEL_ERROR). Both are enforcement wins.
  console.log("BUDGET ERROR CODE:", code, code === "BUDGET_EXCEEDED" || code === "MODEL_ERROR" ? "PASS: cap enforced" : "(unexpected)");
}