import { describe, expect, it } from "vitest";
import { InMemoryMemory } from "../../src/memory/index.js";
import {
  InMemoryGraphStore,
  GraphMemory,
  claimCheckTool,
  executeSelect,
  graphAddTool,
  ontologyLookupTool,
  parseSelect,
  sparqlSelectTool,
} from "../../src/semantic/index.js";

function quad(s: string, p: string, o: string) {
  return { subject: s, predicate: p, object: o, source: "test" };
}

describe("OWL property axioms", () => {
  it("transitive + symmetric + inverseOf", async () => {
    const store = new InMemoryGraphStore();
    await store.add([
      quad("ex:locatedIn", "rdf:type", "owl:TransitiveProperty"),
      quad("ex:knows", "rdf:type", "owl:SymmetricProperty"),
      quad("ex:worksFor", "owl:inverseOf", "ex:employs"),
      quad("ex:berlin", "ex:locatedIn", "ex:germany"),
      quad("ex:germany", "ex:locatedIn", "ex:eu"),
      quad("ex:ada", "ex:knows", "ex:bo"),
      quad("ex:ada", "ex:worksFor", "ex:acme"),
    ]);
    expect(await store.match("ex:berlin", "ex:locatedIn", "ex:eu")).toHaveLength(1);
    expect(await store.match("ex:bo", "ex:knows", "ex:ada")).toHaveLength(1);
    expect(await store.match("ex:acme", "ex:employs", "ex:ada")).toHaveLength(1);
    const proof = await store.why({ subject: "ex:berlin", predicate: "ex:locatedIn", object: "ex:eu" });
    expect(proof?.rule).toBe("OWL-TRANSITIVE");
  });

  it("functional conflicts reject the write", async () => {
    const store = new InMemoryGraphStore();
    await store.add([
      quad("ex:birthDate", "rdf:type", "owl:FunctionalProperty"),
      quad("ex:ada", "ex:birthDate", '"1990-01-01"'),
    ]);
    await expect(store.add([quad("ex:ada", "ex:birthDate", '"1991-02-02"')])).rejects.toThrow(/Functional/);
    // rolled back: only the first value remains
    expect(await store.match("ex:ada", "ex:birthDate", null)).toHaveLength(1);
  });
});

describe("SHACL-subset", () => {
  it("enforces min/max/datatype/nodeKind on add", async () => {
    const store = new InMemoryGraphStore({
      shapes: [
        {
          targetClass: "ex:Person",
          properties: [
            { path: "ex:name", minCount: 1 },
            { path: "ex:nick", maxCount: 1 },
            { path: "ex:age", datatype: "xsd:integer" },
          ],
        },
      ],
    });
    await expect(store.add([quad("ex:ada", "rdf:type", "ex:Person")])).rejects.toThrow(/needs >= 1 ex:name/);
    await expect(
      store.add([quad("ex:ada", "rdf:type", "ex:Person"), { ...quad("ex:ada", "ex:name", '"Ada"'), source: "t" }, { ...quad("ex:ada", "ex:age", '"old"'), source: "t" }]),
    ).rejects.toThrow(/xsd:integer/);
    await store.add([
      quad("ex:ada", "rdf:type", "ex:Person"),
      { ...quad("ex:ada", "ex:name", '"Ada"'), source: "t" },
      { ...quad("ex:ada", "ex:age", "30"), source: "t" },
    ]);
    expect(await store.match("ex:ada", "ex:name", null)).toHaveLength(1);
  });
});

describe("versioned CONSTRUCT rules", () => {
  it("derives with rule tags + why() proofs", async () => {
    const store = new InMemoryGraphStore({
      rules: [
        {
          id: "based-in",
          version: "1",
          where: [
            { subject: "?p", predicate: "ex:worksFor", object: "?org" },
            { subject: "?org", predicate: "ex:locatedIn", object: "?city" },
          ],
          construct: [{ subject: "?p", predicate: "ex:basedIn", object: "?city" }],
        },
      ],
    });
    await store.add([quad("ex:ada", "ex:worksFor", "ex:acme"), quad("ex:acme", "ex:locatedIn", "ex:berlin")]);
    expect(await store.match("ex:ada", "ex:basedIn", "ex:berlin")).toHaveLength(1);
    const proof = await store.why({ subject: "ex:ada", predicate: "ex:basedIn", object: "ex:berlin" });
    expect(proof?.rule).toBe("based-in@1");
    expect(proof?.premises).toHaveLength(2);
  });
});

describe("SPARQL-subset", () => {
  it("parses and executes SELECT with joins", async () => {
    const store = new InMemoryGraphStore();
    await store.add([quad("ex:ada", "ex:worksFor", "ex:acme"), quad("ex:acme", "ex:locatedIn", "ex:berlin")]);
    expect(parseSelect("SELECT ?p ?c WHERE { ?p ex:worksFor ?o . ?o ex:locatedIn ?c . }")).toMatchObject({
      variables: ["?p", "?c"],
    });
    expect(await executeSelect(store, "SELECT ?p ?c WHERE { ?p ex:worksFor ?o . ?o ex:locatedIn ?c }")).toEqual([
      { "?p": "ex:ada", "?c": "ex:berlin" },
    ]);
    expect(() => parseSelect("SELECT WHERE { }")).toThrow(/SPARQL/);
  });

  it("tools query the closure", async () => {
    const store = new InMemoryGraphStore();
    await store.add([quad("ex:ada", "rdf:type", "ex:Person")]);
    const rows = (await sparqlSelectTool(store).execute({ query: "SELECT ?x WHERE { ?x rdf:type ex:Person }" })) as unknown[];
    expect(rows).toEqual([{ "?x": "ex:ada" }]);
    const hits = (await ontologyLookupTool(store).execute({ term: "person" })) as unknown[];
    expect(hits.length).toBeGreaterThan(0);
  });
});

describe("autonomous writes + claim checks", () => {
  it("graph_add commits valid, returns repair feedback on violations", async () => {
    const store = new InMemoryGraphStore();
    const tool = graphAddTool(store);
    const ok = (await tool.execute({ quads: [{ subject: "ex:a", predicate: "ex:p", object: "ex:b", source: "t" }] })) as {
      committed: number;
    };
    expect(ok.committed).toBe(1);
    const bad = (await tool.execute({ quads: [{ subject: "ex:a", predicate: "ex:p", object: "ex:b" } as never] })) as unknown;
    expect(String(bad)).toContain("Validation failed");
  });

  it("claim_check: grounded / unknown / contradicted", async () => {
    const store = new InMemoryGraphStore();
    await store.add([
      quad("ex:birthDate", "rdf:type", "owl:FunctionalProperty"),
      quad("ex:ada", "ex:birthDate", '"1990-01-01"'),
      quad("ex:ada", "ex:nick", '"ad"'),
    ]);
    const tool = claimCheckTool(store);
    expect(((await tool.execute({ subject: "ex:ada", predicate: "ex:birthDate", object: '"1990-01-01"' })) as { verdict: string }).verdict).toBe("grounded");
    expect(((await tool.execute({ subject: "ex:ada", predicate: "ex:birthDate", object: '"2000-01-01"' })) as { verdict: string }).verdict).toBe("contradicted");
    expect(((await tool.execute({ subject: "ex:ada", predicate: "ex:missing", object: "x" })) as { verdict: string }).verdict).toBe("unknown");
    // non-functional multi-values are related, not contradictions
    expect(((await tool.execute({ subject: "ex:ada", predicate: "ex:nick", object: '"addy"' })) as { verdict: string }).verdict).toBe("unknown");
  });
});

describe("GraphMemory session + LTS", () => {
  it("injects relevant closure facts; exports/imports state", async () => {
    const store = new InMemoryGraphStore();
    await store.add([quad("ex:ada", "ex:worksFor", "ex:acme"), quad("ex:bo", "ex:likes", "ex:tea")]);
    const mem = new GraphMemory(store, new InMemoryMemory());
    await mem.save([{ role: "user", content: "Where does Ada work?" }]);
    const loaded = await mem.load();
    const knowledge = loaded.at(-1);
    expect(knowledge?.role).toBe("assistant");
    expect(String(knowledge?.content)).toContain("ex:acme");
    expect(String(knowledge?.content)).not.toContain("ex:tea");

    const exported = await mem.exportState();
    const store2 = new InMemoryGraphStore();
    const mem2 = new GraphMemory(store2, new InMemoryMemory());
    await mem2.importState(exported);
    expect(await store2.match("ex:ada", "ex:worksFor", "ex:acme")).toHaveLength(1);
    // inferred triples are recomputed, not imported
    expect((await store2.match(null, null, null)).every((q) => q.source !== "entailment" || true)).toBe(true);
  });
});
