// LIVE: workflow DAG — topological levels, dependsOn, per-node retry.
// Model calls wrap modelRetryMiddleware: transient network/429/5xx errors
// retry with backoff instead of killing the workflow.
import { Agent, loadConfig, providerFromEnv, runWorkflow, modelRetryMiddleware } from "../../src/index.js";

const cfg = loadConfig();
const model = () => providerFromEnv(cfg.active, { maxTokens: 2000 });
const ask = (name: string, systemPrompt: string) =>
  new Agent({ name, model: model(), systemPrompt, middleware: [modelRetryMiddleware({ maxRetries: 3, initialDelayMs: 500 })] });

// A node that fails ONCE then succeeds — retry policy actually exercised
// with real model latency in the mix.
let flakyCalls = 0;
const flaky = async (): Promise<string> => {
  flakyCalls += 1;
  if (flakyCalls === 1) throw new Error("transient live-run failure");
  return "recovered on retry";
};

const out = await runWorkflow(
  [
    { id: "facts", run: async () => (await ask("facts", "Answer with exactly: water boils")).run("Say your line.") },
    { id: "flaky", run: flaky, retry: { attempts: 2, backoffMs: 100 } },
    { id: "summary", dependsOn: ["facts", "flaky"], run: async (ctx) => ({
        heard: ((ctx.facts as { text?: string })?.text ?? "").trim(),
        flakyStatus: ctx.flaky,
      }) },
  ],
  { invocationState: { run: "live-workflow-demo" } },
);
console.log("CTX facts:", (out.facts as { text?: string })?.text?.trim());
console.log("CTX summary:", out.summary);
console.log("flaky calls:", flakyCalls, "(expect 2: failed once, retried)");
const heard = ((out.summary as { heard?: string } | undefined)?.heard ?? "");
console.log(flakyCalls === 2 && heard.includes("water")
  ? "PASS: DAG executed with real retry + dependency ordering"
  : "NOTE: inspect outputs above");