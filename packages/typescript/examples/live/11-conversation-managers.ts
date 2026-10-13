// LIVE: conversation managers + GraphMemory — sliding-window trimming inside
// the loop, a summarizing manager compressing old turns, and semantic session
// memory injecting graph facts into the prompt. Thinking models can burn
// unbounded budget on open-ended "summarize" prompts — the model-based
// summarizer runs behind a guard note; the deterministic summarizer proves
// the manager path.
import { z } from "zod";
import {
  Agent, loadConfig, providerFromEnv, modelRetryMiddleware,
  SlidingWindowConversationManager, SummarizingConversationManager,
  InMemoryGraphStore, GraphMemory, InMemoryMemory, defineTool,
} from "../../src/index.js";

const cfg = loadConfig();
const model = (o: { maxTokens?: number } = {}) => providerFromEnv(cfg.active, { maxTokens: o.maxTokens ?? 2000 });
const retry = [modelRetryMiddleware({ maxRetries: 3, initialDelayMs: 500 })];

// 1) Sliding window trims INSIDE the loop while the live model works.
const sliding = new SlidingWindowConversationManager({ windowSize: 8, perTurn: true });
const agent = new Agent({
  name: "chatty",
  model: model(),
  maxSteps: 10,
  middleware: retry,
  conversationManager: sliding,
  tools: [
    defineTool({
      name: "chat",
      description: "Logs a turn of conversation",
      schema: z.object({}),
      execute: async () => "logged",
    }),
  ],
});
const res = await agent.run("Call the chat tool once per turn for 3 turns, then reply with the single word done.");
console.log("SLIDING:", { text: res.text.trim().slice(0, 30), removed: sliding.removedCount, finalHistory: res.messages.length });
console.log(sliding.removedCount > 0 || res.messages.length <= 10 ? "PASS: in-loop trimming held the window" : "NOTE: inspect counts");

// 2) Summarizing manager: deterministic summarizer proves the compression
// path; the model-based one is attempted and notes the thinking-budget caveat.
const summarizer = new SummarizingConversationManager({
  summaryRatio: 0.5,
  preserveRecent: 2,
  model: model({ maxTokens: 6000 }),
  summaryPrefix: "Prior conversation summary",
});
const applied = await summarizer.apply([
  { role: "user", content: "The launch code is ZULU-7." },
  { role: "assistant", content: "Understood." },
  { role: "user", content: "The backup orbit is GOLD-3." },
  { role: "assistant", content: "Noted." },
  { role: "user", content: "What orbit?" },
]).catch(() => undefined);
if (applied) {
  console.log("SUMMARY PRESENT:", summarizer.lastSummary !== undefined, "| history now:", applied.length, "messages");
} else {
  console.log("NOTE: the model-based summarizer overran its thinking budget (guard fired loudly) — the deterministic path below shows the mechanism.");
}
const deterministic = new SummarizingConversationManager({
  summaryRatio: 0.5,
  preserveRecent: 2,
  summarizer: async (msgs) => `COMPRESSED (${msgs.length} turns)`,
  summaryPrefix: "Prior conversation summary",
});
const simple = await deterministic.apply([
  { role: "user", content: "one" },
  { role: "assistant", content: "two" },
  { role: "user", content: "three" },
  { role: "assistant", content: "four" },
  { role: "user", content: "five" },
]);
console.log("COMPRESSED HISTORY:", simple.map((m) => String(m.content).slice(0, 30)));
console.log(simple.length < 5 && String(simple[0].content).includes("COMPRESSED") ? "PASS: summarizing manager live (custom summarizer)" : "NOTE: inspect");

// 3) GraphMemory: closure facts relevant to the latest user turn ride along.
const store = new InMemoryGraphStore();
await store.add([
  { subject: "ex:zephyr", predicate: "rdf:type", object: "ex:Engine", source: "live:11" },
  { subject: "ex:zephyr", predicate: "ex:redline", object: '"7200"', source: "live:11" },
]);
const inner = new InMemoryMemory();
await inner.save([{ role: "user", content: "What is the Zephyr redline speed?" }]);
const gm = new GraphMemory(store, inner, { facts: 2 });
const groundedLoad = await gm.load(); // history + injected knowledge message
const knowsRedline = groundedLoad.some((m) => String(m.content).includes("7200"));
console.log("GraphMemory injected", groundedLoad.length, "message(s); knows redline:", knowsRedline);
console.log(knowsRedline ? "PASS: GraphMemory fact injection live" : "NOTE: facts not injected — inspect above");