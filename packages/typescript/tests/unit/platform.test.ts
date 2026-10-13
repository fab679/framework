import { describe, expect, it } from "vitest";
import { parseSkillFile, SkillRegistry } from "../../src/skills/index.js";
import { definePrompt } from "../../src/prompts/index.js";
import { InMemoryGraphStore } from "../../src/semantic/index.js";
import { InMemoryMemory } from "../../src/memory/index.js";
import { CompactedMemory } from "../../src/memory/compaction.js";

describe("skills", () => {
  it("parses SKILL.md frontmatter", () => {
    const skill = parseSkillFile(`---\nname: geo\n description: Geo lookup \nversion: 1.0.0\n---\nDo geo things.`, "geo/SKILL.md");
    expect(skill.name).toBe("geo");
    expect(skill.body).toContain("geo things");
  });

  it("rejects duplicates", () => {
    const reg = new SkillRegistry();
    const s = parseSkillFile(`---\nname: a\ndescription: d\n---\nbody`);
    reg.register({ ...s, tools: [] });
    expect(() => reg.register({ ...s, tools: [] })).toThrow();
  });
});

describe("prompts", () => {
  it("renders vars, throws on missing", () => {
    const p = definePrompt("greet", "Hello {{ name }}!");
    expect(p.render({ name: "Ada" })).toBe("Hello Ada!");
    expect(() => p.render({})).toThrow();
  });
});

describe("semantic", () => {
  it("rejects writes without provenance", async () => {
    const store = new InMemoryGraphStore();
    const report = await store.validate([{ subject: "a", predicate: "b", object: "c" }]);
    expect(report.valid).toBe(false);
  });

  it("accepts provenance-tagged quads", async () => {
    const store = new InMemoryGraphStore();
    await store.add([{ subject: "a", predicate: "b", object: "c", source: "https://ex.com/s" }]);
    expect(await store.match("a", null, null)).toHaveLength(1);
  });
});

describe("compaction", () => {
  it("compacts beyond maxMessages keeping a summary boundary", async () => {
    const mem = new CompactedMemory(new InMemoryMemory(), { maxMessages: 3 });
    await mem.save([
      { role: "user", content: "one" },
      { role: "user", content: "two" },
      { role: "user", content: "three" },
      { role: "user", content: "four" },
    ]);
    const loaded = await mem.load();
    expect(loaded.length).toBeLessThanOrEqual(3);
    expect(loaded[0].content).toContain("Prior context summary");
  });
});
