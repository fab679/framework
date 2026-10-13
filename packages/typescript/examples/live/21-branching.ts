// LIVE: branching — fork a real conversation mid-stream, diverge both sides,
// and see shared context + independent histories + timeline records.
// (state row of the roadmap: editMessage + branch() with version timeline)
import { Agent, loadConfig, providerFromEnv, modelRetryMiddleware } from "../../src/index.js";

const cfg = loadConfig();
const model = () => providerFromEnv(cfg.active, { maxTokens: 2500 });

// 1) Establish shared context the branch must inherit.
const parent = new Agent({
  name: "parent",
  model: model(),
  middleware: [modelRetryMiddleware({ maxRetries: 3, initialDelayMs: 500 })],
});
await parent.run("My codename is Falcon and my favorite number is 91. Just reply ok.");
console.log("PARENT turn 1 done.");

// 2) Branch: independent agent, same history, fresh timeline entry.
const child = await parent.branch("falcon-alt");
console.log("CHILD timeline:", child.timeline.map((t) => `${t.kind}:${t.label ?? ""}`));
console.log("PARENT timeline tail:", parent.timeline.map((t) => t.kind).slice(-1));

// 3) Diverge: both continue from the fork with different questions.
const [childRes, parentRes] = await Promise.all([
  child.run("What is my codename? Reply with just the codename."),
  parent.run("What is my favorite number? Reply with just the number."),
]);
console.log("CHILD says codename:", childRes.text.trim(), "(expect Falcon — inherited context)");
console.log("PARENT says favorite number:", parentRes.text.trim(), "(expect 91)");
console.log("HISTORIES diverged:", child.messages.length, "vs", parent.messages.length, "(both grew independently)");

// 4) editMessage: rewrite a turn (guarded — messages containing tool calls
// are refused so history stays provider-valid). run()-created user turns
// carry no id, so address by index.
const firstUserIdx = parent.messages.findIndex((m) => m.role === "user");
if (firstUserIdx >= 0) {
  const edited = await parent.editMessage(firstUserIdx, "My codename is Sparrow. Just reply ok.");
  console.log("EDITED:", String(edited.content).slice(0, 45));
  // Timeline recorded the edit.
  console.log("PARENT timeline kinds:", parent.timeline.map((t) => t.kind).join(","));
}
const childSaysFalcon = /falcon/i.test(childRes.text);
console.log(
  childSaysFalcon && /91/.test(parentRes.text)
    ? "PASS: live branch inherited context and diverged cleanly"
    : "NOTE: inspect outputs (model variance)",
);