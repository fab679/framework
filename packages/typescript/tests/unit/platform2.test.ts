import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { InMemoryGraphStore } from "../../src/semantic/index.js";
import { InMemoryRetriever, retrievalTool, sparqlRetriever } from "../../src/rag/index.js";
import { runJavascriptTool, createSandboxBackend, matchGlob } from "../../src/sandbox/index.js";
import { abPrompts, definePrompt, loadPromptFile } from "../../src/prompts/index.js";

describe("RAG", () => {
  const docs = [
    { id: "paris", text: "Paris is the capital of France, known for the Eiffel Tower." },
    { id: "tea", text: "Green tea is steamed, preserving catechins and color." },
  ];

  it("TF-IDF ranks relevant docs", async () => {
    const r = new InMemoryRetriever(docs);
    const hits = await r.search("capital France Eiffel");
    expect(hits[0].id).toBe("paris");
    expect(await r.search("quantum bananas")).toEqual([]);
  });

  it("retrievalTool exposes search with validation", async () => {
    const tool = retrievalTool(new InMemoryRetriever(docs));
    const hits = (await tool.execute({ query: "tea catechins" })) as { id: string }[];
    expect(hits[0].id).toBe("tea");
  });

  it("sparqlRetriever reads the closure", async () => {
    const store = new InMemoryGraphStore();
    await store.add([{ subject: "ex:paris", predicate: "ex:capitalOf", object: "ex:france", source: "t" }]);
    const r = sparqlRetriever(store);
    const hits = await r.search("capital france");
    expect(hits[0].text).toContain("ex:paris");
    expect(await r.search("")).toEqual([]);
  });
});

describe("sandbox", () => {
  it("runs expressions + statements, blocks scope escapes, times out", async () => {
    const tool = runJavascriptTool({ timeoutMs: 500 });
    expect(await tool.execute({ code: "1 + 2" })).toBe(3);
    expect(await tool.execute({ code: "let x = 20; x + 22" })).toBe(42);
    expect(await tool.execute({ code: "({ a: 1 })" })).toEqual({ a: 1 });
    await expect(tool.execute({ code: "process.exit(1)" })).rejects.toMatchObject({ code: "SANDBOX_ERROR" });
    await expect(tool.execute({ code: "while(true) {}" })).rejects.toMatchObject({ code: "SANDBOX_TIMEOUT" });
  });
});

describe("sandbox backend", () => {
  it("glob matching", () => {
    expect(matchGlob("**/*.ts", "src/a.ts")).toBe(true);
    expect(matchGlob("**/*.ts", "a.ts")).toBe(true);
    expect(matchGlob("src/*.ts", "src/a.ts")).toBe(true);
    expect(matchGlob("src/*.ts", "src/deep/a.ts")).toBe(false);
    expect(matchGlob("src/**", "src/deep/a.ts")).toBe(true);
    expect(matchGlob("a?c", "abc")).toBe(true);
    expect(matchGlob("a?c", "ac")).toBe(false);
  });

  it("execute + glob + grep + spill + escape rejection", async () => {
    const dir = mkdtempSync(join(tmpdir(), "sandbox-"));
    writeFileSync(join(dir, "hello.txt"), "hello world\nsecond line\n");
    mkdirSync(join(dir, "sub"));
    writeFileSync(join(dir, "sub", "code.ts"), "const x = 1;\n");
    const backend = createSandboxBackend({ root: dir, timeoutMs: 5000, maxOutputChars: 40 });
    const tools = Object.fromEntries(backend.tools().map((t) => [t.name, t]));

    expect(await tools.execute.execute({ command: "echo hi" })).toContain("hi");
    expect(await tools.execute.execute({ command: "exit 3" })).toContain("exit code 3");
    expect(await tools.glob.execute({ pattern: "**/*.txt" })).toContain("hello.txt");
    expect(await tools.grep.execute({ query: "second" })).toContain("hello.txt:2");
    expect(await tools.grep.execute({ query: "zzz-no-match" })).toBe("(no matches)");
    expect(await tools.read_file.execute({ path: "hello.txt" })).toContain("hello");

    // large output spills to a file, secrets are scrubbed from env
    const big = await tools.execute.execute({ command: "yes x | head -c 400" });
    expect(big).toContain(".spill-1.txt");
    expect(big).toContain("read .spill-1.txt with read_file");
    const secret = await tools.execute.execute({ command: "echo SECRET=${SUPER_SECRET_XYZ:-absent}" });
    expect(secret).toContain("absent");
    await expect(tools.read_file.execute({ path: "../outside" })).rejects.toMatchObject({ code: "FS_ESCAPE" });
  }, 20000);
});

describe("prompts files + A/B", () => {
  it("loads frontmatter files, picks variants", () => {
    const dir = mkdtempSync(join(tmpdir(), "prompts-"));
    const path = join(dir, "greet.md");
    writeFileSync(path, `---\nname: greet\nversion: 2.0.0\n---\nHello {{ name }}!`);
    const p = loadPromptFile(path);
    expect(p.render({ name: "Ada" })).toBe("Hello Ada!");
    expect(() => loadPromptFile(join(dir, "missing.md"))).toThrow();
    expect(definePrompt("x", "hi {{ a }} {{ b }}").render({ a: 1, b: 2 })).toBe("hi 1 2");
  });

  it("abPrompts picks explicitly or by weight", () => {
    const variants = [
      { name: "v", template: "A", version: "1" },
      { name: "v", template: "B", version: "2" },
    ];
    expect(abPrompts(variants, "v@2").render({})).toBe("B");
    expect(() => abPrompts(variants, "v@9")).toThrow(/Unknown prompt variant/);
    const only = abPrompts([{ name: "solo", template: "S", weight: 1 }]);
    expect(only.render({})).toBe("S");
  });
});
