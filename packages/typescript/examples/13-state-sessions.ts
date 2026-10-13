import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { z } from "zod";
import {
  Agent,
  InMemoryStorage,
  LocalFileStorage,
  SessionManager,
  SlidingWindowConversationManager,
  defineTool,
} from "../src/index.js";

// Offline demo: three state lifetimes + sessions + conversation managers.
const agent = new Agent({
  model: {
    id: "stub",
    complete: async () => ({
      text: "",
      toolCalls: [{ id: "c1", name: "track", args: { action: "login" } }],
      usage: { inputTokens: 1, outputTokens: 1 },
    }),
  },
  maxSteps: 1,
  state: { logins: 0 },
  conversationManager: new SlidingWindowConversationManager({ windowSize: 20, pinFirst: 1 }),
  sessionManager: new SessionManager({ sessionId: "demo", storage: new InMemoryStorage() }),
  tools: [
    defineTool({
      name: "track",
      description: "Counts actions",
      schema: z.object({ action: z.string() }),
      execute: async ({ action }, ctx) => {
        const n = ((ctx?.state.get("logins") as number) ?? 0) + 1;
        ctx?.state.set("logins", n);
        return `${action} #${n} for ${String(ctx?.invocationState.user_id)}`;
      },
    }),
  ],
});

const res = await agent.run("count my login", { invocationState: { user_id: "u-7" } });
console.log("TOOL:", res.toolCalls[0]?.output);
console.log("STATE:", agent.state.get());
console.log("INVOCATION:", res.invocationState);
console.log("HISTORY:", agent.messages.length, "messages");

// Snapshots: checkpoint, diverge, restore.
const cp = agent.takeSnapshot({ preset: "session", appData: { label: "after-login" } });
agent.state.set("logins", 999);
await agent.loadSnapshot(cp);
console.log("RESTORED logins =", agent.state.get("logins"));

// Sessions survive restarts: same storage, new instance.
const dir = mkdtempSync(join(tmpdir(), "sess-demo-"));
const mk = () =>
  new Agent({
    model: { id: "s", complete: async () => ({ text: "yo", toolCalls: [], usage: { inputTokens: 1, outputTokens: 1 } }) },
    sessionManager: new SessionManager({ sessionId: "demo", storage: new LocalFileStorage(dir) }),
  });
await mk().run("first contact");
const revived = mk();
await revived.run("second contact");
console.log("REVIVED history:", revived.messages.map((m) => `${m.role}:${String(m.content).slice(0, 20)}`));
