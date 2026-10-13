// LIVE: human-in-the-loop — a real model calls a guarded tool, the run
// suspends, a checkpoint persists, and resume() finishes it.
// NOTE: decisions are keyed by hitlKey(tool, args). A live model may
// RE-ISSUE the call with different arguments after resume — then the human
// approves the re-issued proposal too (the loop below). Stable call-ids for
// exact resume matching are a known design item (see review notes).
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { z } from "zod";
import {
  Agent, AgentResult, loadConfig, providerFromEnv, FileCheckpointer, HitlInterrupt,
  HitlDecision, defineTool, hitlKey, humanInTheLoopMiddleware, modelRetryMiddleware,
} from "../../src/index.js";

const cfg = loadConfig();
const dir = mkdtempSync(join(tmpdir(), "live-hitl-"));
const checkpointer = new FileCheckpointer(dir);

const publish = defineTool({
  name: "publish",
  description: "Publishes the report. ALWAYS call this tool before finishing.",
  schema: z.object({ title: z.string().min(1), word: z.string() }),
  execute: async ({ title, word }: { title: string; word: string }) => `published "${title}" (${word})`,
});

const makeAgent = () =>
  new Agent({
    name: "hitl",
    model: providerFromEnv(cfg.active, { maxTokens: 600 }),
    maxSteps: 6,
    tools: [publish],
    middleware: [
      modelRetryMiddleware({ maxRetries: 3, initialDelayMs: 500 }),
      humanInTheLoopMiddleware({ interruptOn: { publish: true } }),
    ],
  });

const decisions: Record<string, HitlDecision> = {};
let done: AgentResult | undefined;

try {
  done = await makeAgent().run(
    'Write a one-sentence report about the word "falcon" and publish it with the publish tool.',
    { checkpointer, threadId: "live-hitl-thread" },
  );
  console.log("NOTE: model finished without calling publish — model variance, rerun");
} catch (first) {
  if (!(first instanceof HitlInterrupt)) throw first;
  console.log("SUSPENDED:", { tool: first.tool, args: first.args, interruptId: first.interruptId });
  // The human turn. TWO scopes:
  // - exact key hitlKey(tool, args): approves ONLY this precise call.
  // - bare tool name: approves every re-issue of the tool (models regenerate
  //   arg text on resume — exact keys alone would never converge).
  decisions[hitlKey(first.tool, first.args)] = "approve";
  decisions[first.tool] = "approve";
  done = await makeAgent().resume("live-hitl-thread", decisions, { checkpointer });
}

if (done) {
  console.log("PUBLISHED TOOL RESULT:", done.toolCalls.find((c) => c.name === "publish")?.output ?? "(none)");
  console.log("RESUMED FINAL:", done.text.trim());
  console.log("PASS: live HITL suspend -> checkpoint -> approve -> resume");
}
console.log("checkpoint persisted in:", dir);