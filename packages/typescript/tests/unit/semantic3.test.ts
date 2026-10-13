/**
 * P2-sem regression/spec tests: remote SPARQL adapter, entity linking +
 * JSON-LD I/O, TBox evolution + conformance battery (roadmap docs/04).
 */
import { describe, expect, it } from "vitest";
import {
  InMemoryGraphStore,
  RemoteSparqlAdapter,
  remoteSparqlTool,
  EntityLinker,
  linkEntityTool,
  MappingProposals,
  proposeMappingTool,
  quadsToJsonLd,
  jsonldToQuads,
  runConformance,
  migrateTBox,
  reviewOntology,
  proposeShapes,
  adoptShapes,
  reviewOntologyTool,
  proposeShapesTool,
  type Quad,
  type NodeShape,
  type OntologyReview,
} from "../../src/semantic/index.js";

function quad(s: string, p: string, o: string): Quad {
  return { subject: s, predicate: p, object: o, source: "test" };
}

// ---------------------------------------------------------------------------
// Remote SPARQL adapter
// ---------------------------------------------------------------------------

describe("RemoteSparqlAdapter", () => {
  const resultsJson = {
    head: { vars: ["iri", "name"] },
    results: {
      bindings: [
        { iri: { value: "http://example.org/Ada" }, name: { value: "Ada Lovelace" } },
        { iri: { value: "http://example.org/Bo" }, name: { value: "Bo" } },
      ],
    },
  };

  it("POSTs sparql-query with auth + pinned prefixes and parses result rows", async () => {
    const seen: { url?: string; init?: RequestInit }[] = [];
    const adapter = new RemoteSparqlAdapter({
      endpoint: "https://sparql.example.org/sparql/",
      auth: { type: "bearer", token: "tok" },
      prefixes: { wd: "http://www.wikidata.org/entity/" },
      fetchFn: async (url, init) => {
        seen.push({ url: String(url), init });
        return new Response(JSON.stringify(resultsJson), { status: 200, headers: { "content-type": "application/sparql-results+json" } });
      },
    });
    const rows = await adapter.select("SELECT ?iri ?name WHERE { ?iri ?p ?name . }");
    expect(rows).toEqual([
      { iri: "http://example.org/Ada", name: "Ada Lovelace" },
      { iri: "http://example.org/Bo", name: "Bo" },
    ]);
    expect(seen[0].url).toBe("https://sparql.example.org/sparql");
    const init = seen[0].init as RequestInit;
    expect((init.headers as Record<string, string>).authorization).toBe("Bearer tok");
    const body = String(init.body);
    expect(body).toContain("PREFIX wd: <http://www.wikidata.org/entity/>");
    expect(body).toContain("SELECT ?iri ?name");
  });

  it("sends UPDATE to the update endpoint; non-retryable 4xx fails immediately", async () => {
    const posted: { url: string; contentType: string }[] = [];
    let updateCalls = 0;
    const adapter = new RemoteSparqlAdapter({
      endpoint: "https://sparql.example.org/sparql",
      updateEndpoint: "https://sparql.example.org/update",
      fetchFn: async (url, init) => {
        posted.push({ url: String(url), contentType: (init?.headers as Record<string, string>)["content-type"] });
        updateCalls += 1;
        if (updateCalls === 1) return new Response("", { status: 200 });
        return new Response("bad update syntax", { status: 400 });
      },
    });
    await adapter.update("INSERT DATA { <urn:a> <urn:b> <urn:c> }");
    expect(posted).toEqual([
      { url: "https://sparql.example.org/update", contentType: "application/sparql-update" },
    ]);
    await expect(adapter.update("INSERT DATA { x }")).rejects.toMatchObject({
      code: "REMOTE_SPARQL",
      retryable: false,
    });
    // Non-retryable: exactly one attempt, no retry loop.
    expect(updateCalls).toBe(2);
  });

  it("select tool is read-only by default", async () => {
    const adapter = new RemoteSparqlAdapter({ endpoint: "https://x/sparql", fetchFn: async () => new Response(JSON.stringify(resultsJson), { status: 200 }) });
    expect(remoteSparqlTool(adapter).name).toBe("remote_sparql_select");
    expect(remoteSparqlTool(adapter, { allowUpdate: true }).name).toBe("remote_sparql");
  });
});

// ---------------------------------------------------------------------------
// Entity linking + sameAs + mapping approvals
// ---------------------------------------------------------------------------

describe("EntityLinker", () => {
  async function linkedStore() {
    const store = new InMemoryGraphStore();
    await store.add([
      quad("ex:ada", "rdf:type", "ex:Person"),
      quad("ex:ada", "rdfs:label", '"Ada Lovelace"'),
      quad("wd:Q7251", "owl:sameAs", "ex:ada"),
      quad("wd:Q7251", "rdfs:label", '"Ada"'),
      quad("ex:bo", "skos:prefLabel", '"Bo"'),
    ]);
    return store;
  }

  it("canonicalizes through sameAs and ranks label matches", async () => {
    const store = await linkedStore();
    const linker = new EntityLinker(store);
    const hits = await linker.link("Ada");
    expect(hits.length).toBeGreaterThanOrEqual(1);
    // wd:Q7251 and ex:ada are the same entity — one canonical answer.
    const iri = hits[0].iri;
    expect(["ex:ada", "wd:Q7251"]).toContain(iri);
    expect(hits[0].sameAsGroup.length).toBe(2);
    expect(hits[0].score).toBeGreaterThanOrEqual(100);
  });

  it("tool links mentions without inventing IRIs", async () => {
    const store = await linkedStore();
    const tool = linkEntityTool(new EntityLinker(store));
    const out = (await tool.execute({ mention: "Bo" }, { agentName: "t", state: new (await import("../../src/state/index.js")).AgentState(), invocationState: {} })) as { iri: string }[];
    expect(out[0].iri).toBe("ex:bo");
  });

  it("review mode (autonomy: false) waits for human approval; only approval writes", async () => {
    const store = await linkedStore();
    const proposals = new MappingProposals();
    // No store bound → nothing to commit against: proposal queues as pending.
    const tool = proposeMappingTool(proposals);
    const proposed = (await tool.execute({ from: "engineer_name", to: "ex:ada", source: "hr:import" }, { agentName: "t", state: new (await import("../../src/state/index.js")).AgentState(), invocationState: {} })) as { id: string };
    // Nothing committed yet — the graph has no alias for the raw mention.
    expect((await new EntityLinker(store).link("engineer_name")).length).toBe(0);

    const approved = await proposals.decide(store, proposed.id, "approved");
    expect(approved?.status).toBe("approved");
    // Approved mapping becomes a provenance-carrying alias the linker sees.
    const linked = await new EntityLinker(store).link("engineer_name");
    expect(linked[0]?.sameAsGroup).toContain("ex:ada");

    const rejected = await proposals.decide(store, (await proposals.propose("x", "ex:bo", "s")).id, "rejected");
    expect(rejected?.status).toBe("rejected");
  });

  it("DEFAULT is autonomous: commits on proposal when a store is bound; review is the opt-out", async () => {
    const store = await linkedStore();
    // Default instance bound to a store: autonomy on.
    const auto = new MappingProposals({ store });
    expect(auto.autonomy).toBe(true);
    const committed = await auto.propose("engineer_name", "ex:ada", "hr:import");
    expect(committed.status).toBe("approved");
    expect(committed.committedByAutonomy).toBe(true);
    // The alias is visible to the linker immediately.
    expect((await new EntityLinker(store).link("engineer_name"))[0]?.sameAsGroup).toContain("ex:ada");
    // Re-proposing the same mapping does not write twice (deduped).
    const again = await auto.propose("engineer_name", "ex:ada", "hr:import");
    expect(again.id).toBe(committed.id);

    // Explicit opt-out queues instead — decide() is the human turn.
    const review = new MappingProposals({ autonomy: false });
    expect(review.autonomy).toBe(false);
    const pending = await review.propose("account_id", "ex:acme", "crm:import");
    expect(pending.status).toBe("pending");
  });
});

// ---------------------------------------------------------------------------
// JSON-LD expand/compact I/O
// ---------------------------------------------------------------------------

describe("JSON-LD I/O", () => {
  it("round-trips quads through expanded JSON-LD", () => {
    const quads = [
      quad("ex:ada", "rdf:type", "ex:Person"),
      quad("ex:ada", "rdfs:label", '"Ada"@en'),
      quad("ex:ada", "ex:birthDate", '"1815-12-10"^^xsd:date'),
      quad("ex:ada", "ex:worksFor", "ex:acme"),
      quad("https://data.example.org/thing", "rdfs:label", '"Full IRI subject"'),
    ];
    const doc = quadsToJsonLd(quads) as { "@graph": Record<string, unknown>[] };
    const ada = doc["@graph"].find((n) => typeof n === "object" && String(n["@id"]).endsWith("ada")) as Record<string, unknown>;
    // Expanded form: absolute-IRI predicate keys, IRIs as {"@id": ...}.
    expect(String(ada["@id"])).toBe("urn:example:ada");
    expect(ada["@type"]).toBe("urn:example:Person");
    expect(ada["http://www.w3.org/2000/01/rdf-schema#label"]).toEqual({ "@value": "Ada", "@language": "en" });

    const back = jsonldToQuads({ "@graph": doc["@graph"] } as never);
    const sorted = (xs: { subject: string; predicate: string; object: string }[]) =>
      xs.map((q) => `${q.subject}|${q.predicate}|${q.object}`).sort();
    expect(sorted(back)).toEqual(sorted(quads));
  });

  it("round-trips NAMED GRAPHS as JSON-LD named-graph entries", () => {
    const quads = [
      { ...quad("ex:Employee", "rdfs:subClassOf", "ex:Person"), graph: "urn:framework:ontology:v4" },
      quad("ex:ada", "rdf:type", "ex:Employee"), // default graph
    ];
    const doc = quadsToJsonLd(quads) as { "@graph": Record<string, unknown>[] };
    const named = doc["@graph"].find((n) => Array.isArray(n["@graph"]));
    expect(String(named!["@id"])).toBe("urn:framework:ontology:v4");
    const back = jsonldToQuads({ "@graph": doc["@graph"] } as never);
    expect(back).toHaveLength(2);
    expect(back.find((q) => q.predicate === "rdfs:subClassOf")?.graph).toBe("urn:framework:ontology:v4");
    expect(back.find((q) => q.predicate === "rdf:type")?.graph).toBeUndefined();
  });
});

describe("named graph identity (store)", () => {
  it("same triple in two graphs are distinct quads; match() filters by graph", async () => {
    const store = new InMemoryGraphStore();
    await store.add([
      { ...quad("ex:ada", "ex:worksFor", "ex:acme"), graph: "urn:test:hr" },
      { ...quad("ex:ada", "ex:worksFor", "ex:acme"), graph: "urn:test:legacy" },
    ]);
    expect(await store.match("ex:ada", "ex:worksFor", "ex:acme")).toHaveLength(2);
    expect(await store.match("ex:ada", "ex:worksFor", "ex:acme", "urn:test:hr")).toHaveLength(1);
  });

  it("migration writes the v4 TBox into its own named graph, keeps ABox free", async () => {
    const store = new InMemoryGraphStore();
    await store.add([
      quad("ex:Employee", "rdfs:subClassOf", "ex:Person"),
      quad("ex:worksFor", "rdfs:domain", "ex:Person"),
      quad("ex:ada", "rdf:type", "ex:Employee"),
      quad("ex:ada", "ex:worksFor", "ex:acme"),
    ]);
    const { report, store: v4 } = await migrateTBox(store, { from: "v3", to: "v4" }, { battery: [] });
    expect(report.tboxGraph).toBe("urn:framework:ontology:v4");
    const tbox = await v4!.match(null, null, null, "urn:framework:ontology:v4");
    // Ontology statements only in the version graph...
    expect(tbox.some((q) => q.predicate === "rdfs:subClassOf")).toBe(true);
    expect(tbox.some((q) => q.predicate === "rdfs:domain")).toBe(true);
    // ...while instance facts stay in the default graph.
    expect(tbox.some((q) => q.subject === "ex:ada")).toBe(false);
    expect(await v4!.match("ex:ada", "rdf:type", "ex:Employee")).toHaveLength(1);
    // Opt-out keeps everything graph-free.
    const plain = await migrateTBox(store, { from: "v3", to: "v5" }, { battery: [], tboxGraph: false });
    expect(plain.report.tboxGraph).toBeUndefined();
    expect((await plain.store!.match(null, null, null, "urn:framework:ontology:v5")).length).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// TBox evolution + conformance battery
// ---------------------------------------------------------------------------

describe("conformance battery", () => {
  it("ask checks pass/fail on row counts; shapes validate", async () => {
    const store = new InMemoryGraphStore();
    await store.add([quad("ex:ada", "rdf:type", "ex:Person")]);
    const report = await runConformance(store, [
      { id: "has-person", ask: "SELECT ?s WHERE { ?s rdf:type ex:Person . }" },
      { id: "no-company", ask: "SELECT ?s WHERE { ?s rdf:type ex:Company . }", minRows: 0, maxRows: 0 },
      {
        id: "person-needs-name",
        shape: { targetClass: "ex:Person", properties: [{ path: "ex:name", minCount: 1 }] },
      },
    ]);
    expect(report.results.find((r) => r.id === "has-person")?.passed).toBe(true);
    expect(report.results.find((r) => r.id === "no-company")?.passed).toBe(true);
    const shapeCheck = report.results.find((r) => r.id === "person-needs-name");
    expect(shapeCheck?.passed).toBe(false);
    expect(shapeCheck?.detail).toContain("ex:name");
    expect(report.passed).toBe(false);
  });
});

describe("migrateTBox", () => {
  async function v3Store() {
    const store = new InMemoryGraphStore({
      shapes: [{ targetClass: "ex:Person", properties: [{ path: "ex:name", minCount: 1 }] }],
    });
    await store.add([
      quad("ex:Employee", "rdfs:subClassOf", "ex:Person"),
      quad("ex:ada", "rdf:type", "ex:Employee"),
      quad("ex:ada", "ex:name", '"Ada"'),
      quad("ex:ada", "ex:worksAt", "ex:acme"),
      quad("ex:acme", "ex:name", '"Acme"'),
    ]);
    return store;
  }

  const battery = [
    { id: "persons-exist", ask: "SELECT ?s WHERE { ?s rdf:type ex:Employee . }" },
  ];

  it("renames a predicate, rewrites facts + closure, keeps the old store intact", async () => {
    const old = await v3Store();
    const { report, store: next } = await migrateTBox(old, {
      from: "v3",
      to: "v4",
      renames: [{ from: "ex:worksAt", to: "ex:worksFor" }],
      added: [quad("ex:Manager", "rdfs:subClassOf", "ex:Employee")],
    }, { battery });
    expect(report.status).toBe("migrated");
    expect(report.rewrittenQuads).toBe(1);
    expect(report.addedQuads).toBe(1);
    // Old store intact (rollback free).
    expect(await old.match("ex:ada", "ex:worksAt", null)).toHaveLength(1);
    // New store: renamed predicate present, old one gone.
    expect(await next!.match("ex:ada", "ex:worksFor", "ex:acme")).toHaveLength(1);
    expect(await next!.match("ex:ada", "ex:worksAt", null)).toHaveLength(0);
    // New axiom entailed into the closure: Manager isa Person via Employee.
    expect(report.postBattery?.passed).toBe(true);
  });

  it("rewrites deprecated terms to replacements and drops removed axioms", async () => {
    const old = await v3Store();
    const { report, store: next } = await migrateTBox(old, {
      from: "v3",
      to: "v4",
      removed: [quad("ex:Employee", "rdfs:subClassOf", "ex:Person")],
      deprecations: [{ term: "ex:worksAt", replacement: "ex:worksFor", reason: "consistency" }],
    }, { battery: [] });
    expect(report.removedQuads).toBe(1);
    // Removed axiom: ada is no longer a Person by entailment.
    expect(await next!.match("ex:ada", "rdf:type", "ex:Person")).toHaveLength(0);
    expect(await next!.match("ex:ada", "ex:worksFor", "ex:acme")).toHaveLength(1);
  });

  it("aborts when the pre-battery fails, fails the migration on post-battery", async () => {
    const old = await v3Store();
    const aborted = await migrateTBox(old, { from: "v3", to: "v4" }, { battery: [] });
    expect(aborted.report.status).toBe("migrated");

    // Impossible pre-check: no such class yet → abort without touching.
    const impossible = [{ id: "needs-manager", ask: "SELECT ?s WHERE { ?s rdf:type ex:Manager . }", minRows: 1 }];
    const pre = await migrateTBox(old, { from: "v3", to: "v4", added: [quad("ex:Manager", "rdfs:subClassOf", "ex:Employee")] }, { battery: impossible });
    expect(pre.report.status).toBe("aborted");
    expect(pre.store).toBeUndefined();

    // Failing post-check: renamed the class so the battery's ask finds nothing.
    const stale = [...battery];
    const post = await migrateTBox(old, {
      from: "v3",
      to: "v4",
      renames: [{ from: "ex:Employee", to: "ex:Staff" }],
    }, { battery: stale });
    expect(post.report.status).toBe("failed");
    expect(post.store).toBeUndefined();
    // And the old store still satisfies the battery — nothing was destroyed.
    const recheck = await runConformance(old, stale);
    expect(recheck.passed).toBe(true);
  });
});
// ---------------------------------------------------------------------------
// Ontology self-review + SHACL induction
// ---------------------------------------------------------------------------

describe("ontology review + shape induction", () => {
  async function reviewedStore() {
    const store = new InMemoryGraphStore();
    await store.add([
      // Declared, used, labeled term.
      quad("ex:Person", "rdfs:subClassOf", "ex:Thing"),
      quad("ex:Person", "rdfs:label", '"Person"'),
      quad("ex:worksFor", "rdfs:domain", "ex:Person"), // domain but NO range
      quad("ex:name", "rdfs:range", "xsd:string"),     // range but NO domain
      quad("ex:Ghost", "rdfs:subClassOf", "ex:Thing"), // declared, never used
      // Instance data.
      quad("ex:ada", "rdf:type", "ex:Person"),
      quad("ex:bo", "rdf:type", "ex:Person"),
      quad("ex:ada", "ex:name", '"Ada"'),
      quad("ex:bo", "ex:name", '"Bo"'),
      quad("ex:ada", "ex:worksFor", "ex:acme"),
      quad("ex:bo", "ex:worksFor", "ex:acme"),
      // Used in data, never declared anywhere.
      quad("ex:ada", "ex:nickname", '"Countess"'),
    ]);
    return store;
  }

  it("reviewOntology inventories terms and reports gaps", async () => {
    const review = await reviewOntology(await reviewedStore());
    // ex:nickname: used-but-undeclared — the "what to add" list.
    expect(review.undeclared.map((t) => t.term)).toContain("ex:nickname");
    expect(review.undeclared.map((t) => t.term)).not.toContain("ex:name");
    // ex:Ghost: declared but never used.
    expect(review.unused).toContain("ex:Ghost");
    // ex:worksFor has a domain but no range; ex:name has neither typed uses...
    expect(review.missingRange).toContain("ex:worksFor");
    expect(review.missingDomain).toContain("ex:name");
    // Vocabulary predicates never show up as fake "properties".
    expect(review.properties.map((p) => p.term)).not.toContain("rdf:type");
    // Label inventory works.
    const person = review.classes.find((c) => c.term === "ex:Person");
    expect(person?.label).toBe("Person");
    expect(person?.uses).toBe(2);
    expect(review.closureQuads).toBeGreaterThanOrEqual(review.explicitQuads);
  });

  it("induces SHACL shapes from instance data (proposal, not adoption)", async () => {
    const store = await reviewedStore();
    const shapes = await proposeShapes(store);
    const person = shapes.find((s) => s.targetClass === "ex:Person");
    expect(person).toBeDefined();
    const nameShape = person!.properties.find((p) => p.path === "ex:name");
    expect(nameShape).toMatchObject({ minCount: 1, maxCount: 1, datatype: "xsd:string" });
    const worksFor = person!.properties.find((p) => p.path === "ex:worksFor");
    expect(worksFor).toMatchObject({ minCount: 1, nodeKind: "iri" });
    // ex:nickname: 1 of 2 instances = 50% coverage -> included at default 0.5.
    expect(person!.properties.some((p) => p.path === "ex:nickname")).toBe(true);
    // Proposal did NOT touch the store's shape list — builder owns adoption.
    expect(store.ontologyShapes()).toHaveLength(0);
  });

  it("adoptShapes registers clean shapes and rejects ones the data violates", async () => {
    const store = await reviewedStore();
    // ex:name minCount 1 holds; a hostile "every Person must have ex:nickname"
    // shape (only ada has it) must be rejected with its violations.
    const { adopted, rejected } = await adoptShapes(store, [
      { targetClass: "ex:Person", properties: [{ path: "ex:name", minCount: 1 }] },
      { targetClass: "ex:Person", properties: [{ path: "ex:nickname", minCount: 1 }] },
    ]);
    expect(adopted.map((s) => s.targetClass)).toEqual(["ex:Person"]);
    expect(rejected).toHaveLength(1);
    expect(rejected[0].violations[0]).toContain("ex:bo");
    // The adopted shape now enforces future writes.
    expect(store.ontologyShapes()).toHaveLength(1);
  });

  it("tools expose review + proposals to agents", async () => {
    const store = await reviewedStore();
    const review = await reviewOntologyTool(store).execute({}, { agentName: "t", state: new (await import("../../src/state/index.js")).AgentState(), invocationState: {} }) as OntologyReview;
    expect(review.undeclared.map((t) => t.term)).toContain("ex:nickname");
    const proposals = await proposeShapesTool(store, { coverage: 1 }).execute({}, { agentName: "t", state: new (await import("../../src/state/index.js")).AgentState(), invocationState: {} }) as NodeShape[];
    // coverage: 1 (every instance) drops ex:nickname from the proposal.
    const person = proposals.find((s) => s.targetClass === "ex:Person");
    expect(person!.properties.some((p) => p.path === "ex:nickname")).toBe(false);
    expect(person!.properties.some((p) => p.path === "ex:name")).toBe(true);
  });
});

describe("RemoteSparqlAdapter resilience", () => {
  it("retries 429 honoring Retry-After, then succeeds", async () => {
    let calls = 0;
    const starts: number[] = [];
    const adapter = new RemoteSparqlAdapter({
      endpoint: "https://sparql.example.org/sparql",
      maxRetries: 2,
      fetchFn: async () => {
        calls += 1;
        starts.push(Date.now());
        if (calls === 1) {
          return new Response("", { status: 429, headers: { "retry-after": "1" } });
        }
        return new Response(
          JSON.stringify({ head: { vars: ["x"] }, results: { bindings: [{ x: { value: "urn:a" } }] } }),
          { status: 200 },
        );
      },
    });
    const rows = await adapter.select("SELECT ?x WHERE { ?s ?p ?x }");
    expect(calls).toBe(2);
    expect(rows).toEqual([{ x: "urn:a" }]);
    expect(Date.now() - starts[0]).toBeGreaterThanOrEqual(900); // honored ~1s Retry-After
  }, 10000);

  it("throws immediately on non-retryable 4xx (no retry loop)", async () => {
    let calls = 0;
    const adapter = new RemoteSparqlAdapter({
      endpoint: "https://sparql.example.org/sparql",
      fetchFn: async () => {
        calls += 1;
        return new Response("bad query", { status: 400 });
      },
    });
    await expect(adapter.select("SELECT ?x WHERE { broken")).rejects.toMatchObject({
      code: "REMOTE_SPARQL",
      retryable: false,
    });
    expect(calls).toBe(1);
  });

  it("sends a descriptive User-Agent by default (public endpoints 403 without one)", async () => {
    let seenHeaders: Record<string, string> = {};
    const adapter = new RemoteSparqlAdapter({
      endpoint: "https://sparql.example.org/sparql",
      fetchFn: async (_u, init) => {
        seenHeaders = (init?.headers ?? {}) as Record<string, string>;
        return new Response(JSON.stringify({ head: { vars: [] }, results: { bindings: [] } }), { status: 200 });
      },
    });
    await adapter.select("SELECT ?x WHERE { ?s ?p ?x }");
    expect(seenHeaders["user-agent"]).toMatch(/agent-framework/);
  });
});
