import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { Agent } from "../../src/agent/index.js";
import { InMemoryMemory } from "../../src/memory/index.js";
import { InMemoryStorage, LocalFileStorage, SessionManager } from "../../src/sessions/index.js";
import type { ModelCompletion } from "../../src/models/index.js";

function textReply(text: string) {
  return {
    id: "t",
    complete: async (): Promise<ModelCompletion> => ({ text, toolCalls: [], usage: { inputTokens: 1, outputTokens: 1 } }),
  };
}

describe("storage backends", () => {
  it("in-memory round-trips + lists + deletes", async () => {
    const s = new InMemoryStorage();
    expect(await s.read("missing")).toBeUndefined();
    await s.write("a/1.json", "{}");
    await s.write("a/2.json", "{}");
    expect(await s.list("a/")).toHaveLength(2);
    await s.deletePrefix("a/");
    expect(await s.list("a/")).toEqual([]);
  });

  it("local files round-trip", async () => {
    const s = new LocalFileStorage(mkdtempSync(join(tmpdir(), "sess-")));
    expect(await s.read("x/y.json")).toBeUndefined();
    await s.write("x/y.json", '{"a":1}');
    expect(await s.read("x/y.json")).toBe('{"a":1}');
    expect(await s.list("x/")).toHaveLength(1);
  });
});

describe("sessions", () => {
  it("persists + restores across agent instances", async () => {
    const storage = new InMemoryStorage();
    const mk = () =>
      new Agent({
        model: textReply("yo"),
        memory: new InMemoryMemory(),
        state: {},
        sessionManager: new SessionManager({ sessionId: "s1", storage }),
      });
    const a = mk();
    expect(a.sessionId).toBe("s1");
    a.state.set("theme", "dark");
    await a.run("hello");
    const b = mk();
    await b.run("second");
    expect(b.state.get("theme")).toBe("dark");
    expect(b.messages.some((m) => m.content === "hello")).toBe(true);
  });

  it("snapshots: field selection, appData, restore, immutable history", async () => {
    const storage = new InMemoryStorage();
    const sm = new SessionManager({ sessionId: "s2", storage });
    const agent = new Agent({ model: textReply("hi"), sessionManager: sm });
    await agent.run("hello");
    agent.state.set("k", "v");

    const full = agent.takeSnapshot({ preset: "session", appData: { label: "cp1" } });
    expect(full.appData).toEqual({ label: "cp1" });
    expect(full.data.messages?.length).toBeGreaterThan(0);
    expect(full.data.state).toEqual({ k: "v" });

    const msgsOnly = agent.takeSnapshot({ include: ["messages"] });
    expect(msgsOnly.data.state).toBeUndefined();

    const id = await sm.saveSnapshot(full.data, full.appData);
    expect(await sm.listSnapshotIds()).toEqual([id]);
    const restored = await sm.restoreSnapshot(id);
    expect(restored?.appData).toEqual({ label: "cp1" });

    const fresh = new Agent({ model: textReply("x") });
    await fresh.loadSnapshot(full);
    expect(fresh.messages.length).toBe(agent.messages.length);
    expect(fresh.state.get("k")).toBe("v");

    await sm.deleteSession();
    expect(await sm.listSnapshotIds()).toEqual([]);
    expect(new Agent({ model: textReply("x") }).sessionId).toMatch(/^[0-9a-f]{8}$/);
  });
});
