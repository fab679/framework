import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { A2AClient } from "../../src/a2a/index.js";
import { buildServedAgent, loadFrameworkConfig, serveFramework } from "../../src/serve/index.js";

function writeConfig(dir: string): string {
  mkdirSync(join(dir, "skills", "demo"), { recursive: true });
  writeFileSync(join(dir, "skills", "demo", "SKILL.md"), `---\nname: demo\ndescription: demo skill\n---\nBody.`);
  mkdirSync(join(dir, "agents"));
  writeFileSync(
    join(dir, "agents", "filed.json"),
    JSON.stringify({ name: "filed", model: "ollama:qwen2.5", maxSteps: 3 }),
  );
  writeFileSync(join(dir, "agent.env"), "FW_TEST_MARKER=from-agent-env\n");
  const path = join(dir, "framework.json");
  writeFileSync(
    path,
    JSON.stringify({
      server: { host: "127.0.0.1", basePort: 0 },
      envFile: "./shared.env",
      env: { FW_TEST_INLINE: "yes" },
      checkpointer: { dir: "./checkpoints" },
      http: { corsOrigins: ["https://app.example.com"] },
      agents: [
        { name: "a1", model: "ollama:qwen2.5", skills: ["./skills"], filesystem: "./ws", todos: true, sessionDir: "./sess" },
        { file: "./agents/filed.json" },
        { file: "./agents/filed.json", name: "filed-override", envFile: "./agent.env" },
      ],
    }),
  );
  writeFileSync(join(dir, "shared.env"), "FW_TEST_MARKER=from-shared-env\n");
  return path;
}

describe("framework.json", () => {
  it("loads, resolves paths, builds agents", async () => {
    const dir = mkdtempSync(join(tmpdir(), "fw-"));
    const file = loadFrameworkConfig(writeConfig(dir));
    expect(file.agents).toHaveLength(3);
    expect(file.agents[0].skills[0]).toContain("skills");
    expect(file.agents[0].filesystem).toContain("ws");
    // file refs resolve relative to their own dir; inline keys override
    expect(file.agents[1].name).toBe("filed");
    expect(file.agents[2].name).toBe("filed-override");
    // per-agent envFile wins over framework-level
    expect(process.env.FW_TEST_MARKER).toBe("from-agent-env");
    expect(process.env.FW_TEST_INLINE).toBe("yes");
    expect(file.checkpointer?.dir).toContain("checkpoints");
    expect(file.http.corsOrigins).toEqual(["https://app.example.com"]);
    delete process.env.FW_TEST_MARKER;
    delete process.env.FW_TEST_INLINE;
    const { agent, card } = await buildServedAgent(file.agents[0]);
    expect(agent.name).toBe("a1");
    expect(card.name).toBe("a1");
    expect(agent.tools.map((t) => t.name)).toContain("write_todos");
    expect(agent.tools.map((t) => t.name)).toContain("read_file");
  });

  it("rejects invalid configs", () => {
    const dir = mkdtempSync(join(tmpdir(), "fw-"));
    const bad = join(dir, "bad.json");
    writeFileSync(bad, JSON.stringify({ agents: [] }));
    expect(() => loadFrameworkConfig(bad)).toThrow();
  });

  it("serves agents over A2A", async () => {
    const dir = mkdtempSync(join(tmpdir(), "fw-"));
    const file = loadFrameworkConfig(writeConfig(dir));
    const { agents, stop } = await serveFramework(file);
    try {
      expect(agents).toHaveLength(3);
      expect(new Set(agents.map((a) => a.url)).size).toBe(3);
      const card = await new A2AClient(agents[0].url).getCard();
      expect(card.name).toBe("a1");
    } finally {
      await stop();
    }
  });
});
