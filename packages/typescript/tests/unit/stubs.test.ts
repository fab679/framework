import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterAll, describe, expect, it } from "vitest";
import { discoverSkills, loadSkillsInto, matchSkills, SkillRegistry } from "../../src/skills/index.js";
import { InMemoryGraphStore } from "../../src/semantic/index.js";
import { jsonSchemaToZod, mcpToolsToFramework, McpHttpClient, McpStdioClient, connectMcpHttp, connectMcpStdio } from "../../src/mcp/index.js";
import { createServer, type Server } from "node:http";

function quad(s: string, p: string, o: string) {
  return { subject: s, predicate: p, object: o, source: "test" };
}

describe("skills discovery + matching", () => {
  it("discovers SKILL.md dirs, skips missing/invalid", async () => {
    const root = mkdtempSync(join(tmpdir(), "skills-"));
    mkdirSync(join(root, "geo"));
    writeFileSync(join(root, "geo", "SKILL.md"), `---\nname: geo\ndescription: geocode places\n---\nDo geo.`);
    mkdirSync(join(root, "bad"));
    writeFileSync(join(root, "bad", "SKILL.md"), "no frontmatter here");
    const found = await discoverSkills([join(root, "missing"), root]);
    expect(found.map((s) => s.name)).toEqual(["geo"]);
    await expect(discoverSkills([root], { strict: true })).rejects.toThrow();
    const reg = new SkillRegistry();
    await loadSkillsInto(reg, [root]);
    expect(reg.get("geo")?.body).toContain("Do geo");
  });

  it("matchSkills scores overlap, $mention always wins", () => {
    const reg = new SkillRegistry();
    reg.register({ name: "geo", description: "geocode places", version: "1", path: "g", body: "b" });
    reg.register({ name: "pdf", description: "fill forms", version: "1", path: "p", body: "b" });
    expect(matchSkills(reg, "geocode this place").map((s) => s.name)).toEqual(["geo"]);
    expect(matchSkills(reg, "unrelated xyz").map((s) => s.name)).toEqual([]);
    expect(matchSkills(reg, "do $pdf please").map((s) => s.name)).toEqual(["pdf"]);
  });
});

describe("semantic RDFS entailment", () => {
  it("subClassOf transitivity + type propagation + why()", async () => {
    const store = new InMemoryGraphStore();
    await store.add([
      quad("ex:Engineer", "rdfs:subClassOf", "ex:Person"),
      quad("ex:Person", "rdfs:subClassOf", "ex:Agent"),
      quad("ex:ada", "rdf:type", "ex:Engineer"),
    ]);
    expect(await store.match("ex:Engineer", "rdfs:subClassOf", "ex:Agent")).toHaveLength(1);
    expect(await store.match("ex:ada", "rdf:type", "ex:Agent")).toHaveLength(1);
    const proof = await store.why({ subject: "ex:ada", predicate: "rdf:type", object: "ex:Agent" });
    expect(proof?.rule).toBe("RDFS-TYPE");
    expect(proof?.premises).toHaveLength(2);
    expect(await store.why({ subject: "ex:ada", predicate: "rdf:type", object: "ex:Engineer" })).toBeNull();
  });

  it("subPropertyOf propagation", async () => {
    const store = new InMemoryGraphStore();
    await store.add([
      quad("ex:worksFor", "rdfs:subPropertyOf", "ex:affiliatedWith"),
      quad("ex:ada", "ex:worksFor", "ex:acme"),
    ]);
    expect(await store.match("ex:ada", "ex:affiliatedWith", "ex:acme")).toHaveLength(1);
  });

  it("domain + range inference", async () => {
    const store = new InMemoryGraphStore();
    await store.add([
      quad("ex:worksFor", "rdfs:domain", "ex:Person"),
      quad("ex:worksFor", "rdfs:range", "ex:Org"),
      quad("ex:ada", "ex:worksFor", "ex:acme"),
    ]);
    expect(await store.match("ex:ada", "rdf:type", "ex:Person")).toHaveLength(1);
    expect(await store.match("ex:acme", "rdf:type", "ex:Org")).toHaveLength(1);
  });

  it("dedups facts on re-add", async () => {
    const store = new InMemoryGraphStore();
    await store.add([quad("ex:a", "ex:p", "ex:b")]);
    await store.add([quad("ex:a", "ex:p", "ex:b")]);
    expect(await store.match("ex:a", "ex:p", "ex:b")).toHaveLength(1);
  });

  it("supports full-URI predicates", async () => {
    const store = new InMemoryGraphStore();
    const T = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type";
    const S = "http://www.w3.org/2000/01/rdf-schema#subClassOf";
    await store.add([
      { subject: "ex:Dog", predicate: S, object: "ex:Animal", source: "t" },
      { subject: "ex:fido", predicate: T, object: "ex:Dog", source: "t" },
    ]);
    expect(await store.match("ex:fido", T, "ex:Animal")).toHaveLength(1);
  });
});

describe("jsonSchemaToZod", () => {
  it("converts objects/enums/arrays/optionals", () => {
    const schema = jsonSchemaToZod({
      type: "object",
      properties: {
        city: { type: "string" },
        units: { enum: ["c", "f"] },
        tags: { type: "array", items: { type: "string" } },
        count: { type: "integer" },
      },
      required: ["city"],
    });
    expect(schema.safeParse({ city: "SF", units: "c", tags: [], count: 2 }).success).toBe(true);
    expect(schema.safeParse({}).success).toBe(false);
    expect(schema.safeParse({ city: "SF", units: "x" }).success).toBe(false);
  });
});

describe("MCP HTTP client", () => {
  let server: Server;
  let base: string;
  afterAll(() => new Promise((r) => server?.close(() => r(undefined))));

  it("handshakes, lists, calls tools", async () => {
    server = createServer((req, res) => {
      let body = "";
      req.on("data", (c) => (body += c));
      req.on("end", () => {
        const msg = JSON.parse(body) as { id: number; method: string; params: { name?: string; arguments?: unknown } };
        const reply = (result: unknown) => {
          res.writeHead(200, { "content-type": "application/json" });
          res.end(JSON.stringify({ jsonrpc: "2.0", id: msg.id, result }));
        };
        if (msg.method === "initialize") reply({ protocolVersion: "2024-11-05", serverInfo: { name: "fake", version: "1" } });
        else if (msg.method === "notifications/initialized") reply({});
        else if (msg.method === "tools/list")
          reply({ tools: [{ name: "echo", description: "echoes", inputSchema: { type: "object", properties: { v: { type: "string" } }, required: ["v"] } }] });
        else if (msg.method === "tools/call" && msg.params.name === "boom")
          reply({ content: [{ type: "text", text: "kaput" }], isError: true });
        else if (msg.method === "tools/call")
          reply({ content: [{ type: "text", text: `echo:${JSON.stringify(msg.params.arguments)}` }] });
        else {
          res.writeHead(200, { "content-type": "application/json" });
          res.end(JSON.stringify({ jsonrpc: "2.0", id: msg.id, error: { code: -32601, message: "nope" } }));
        }
      });
    });
    await new Promise<void>((r) => server.listen(0, "127.0.0.1", () => r()));
    base = `http://127.0.0.1:${(server.address() as { port: number }).port}`;

    const client = new McpHttpClient(base);
    const info = await client.connect();
    expect(info.name).toBe("fake");
    const tools = await client.listTools();
    expect(tools.map((t) => t.name)).toEqual(["echo"]);
    expect(await client.callTool("echo", { v: "hi" })).toBe('echo:{"v":"hi"}');
    await expect(client.callTool("boom", {})).rejects.toMatchObject({ code: "MCP_TOOL_ERROR" });

    const [tool] = await mcpToolsToFramework(client);
    expect(tool.name).toBe("echo");
    expect(await tool.execute({ v: "yo" })).toBe('echo:{"v":"yo"}');
    expect(() => tool.schema.parse({})).toThrow();
    await client.close();
  });

  it("requires connect() and surfaces RPC errors", async () => {
    const client = new McpHttpClient("http://127.0.0.1:1");
    await expect(client.listTools()).rejects.toMatchObject({ code: "MCP_NOT_CONNECTED" });
  });

  it("stdio transport round-trips + helpers connect", async () => {
    const serverJs = `let b="";process.stdin.on("data",c=>{b+=c;const l=b.split("\\n");b=l.pop();for(const t of l){if(!t.trim())continue;const m=JSON.parse(t);const r=(r2)=>process.stdout.write(JSON.stringify({jsonrpc:"2.0",id:m.id,result:r2})+"\\n");if(m.method==="initialize")r({serverInfo:{name:"stdio-fake"}});else if(m.method==="tools/list")r({tools:[{name:"ping",description:"p",inputSchema:{type:"object"}}]});else if(m.method==="tools/call")r({content:[{type:"text",text:"pong"}]});else process.stdout.write(JSON.stringify({jsonrpc:"2.0",id:m.id,error:{code:-32601,message:"no"}})+"\\n");}});`;
    const stdio = new McpStdioClient(process.execPath, ["-e", serverJs]);
    await stdio.connect();
    expect((await stdio.listTools()).map((t) => t.name)).toEqual(["ping"]);
    expect(await stdio.callTool("ping", {})).toBe("pong");
    const [tool] = await mcpToolsToFramework(stdio);
    expect(tool.name).toBe("ping");
    await stdio.close();

    const http = await connectMcpHttp("http://127.0.0.1:1").catch(() => undefined);
    expect(http).toBeUndefined();
    const bad = new McpStdioClient("definitely-not-a-real-command-xyz");
    await expect(bad.connect()).rejects.toThrow();
    await bad.close();
  }, 20000);

  it("connectMcpStdio helper connects", async () => {
    const serverJs = `let b="";process.stdin.on("data",c=>{b+=c;const l=b.split("\\n");b=l.pop();for(const t of l){if(!t.trim())continue;const m=JSON.parse(t);process.stdout.write(JSON.stringify({jsonrpc:"2.0",id:m.id,result:{}})+"\\n");}});`;
    const client = await connectMcpStdio(process.execPath, ["-e", serverJs]);
    await client.close();
  }, 20000);
});
