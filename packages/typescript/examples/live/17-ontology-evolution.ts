// LIVE: ontology evolution end-to-end — a live model commits facts, self-review
// lists the gaps, then a v3->v4 TBox migration (rename + deprecation + battery)
// rebuilds the store with axioms scoped to their own named graph.
import {
  Agent, loadConfig, providerFromEnv, modelRetryMiddleware,
  InMemoryGraphStore, migrateTBox, runConformance, reviewOntology,
  graphAddTool, quadsToJsonLd,
} from "../../src/index.js";

const cfg = loadConfig();

// v3: the model commits instance data under an OLD predicate (emp_name).
const v3 = new InMemoryGraphStore();
const setup = new Agent({
  name: "writer",
  model: providerFromEnv(cfg.active, { maxTokens: 1000 }),
  middleware: [modelRetryMiddleware({ maxRetries: 3, initialDelayMs: 500 })],
  maxSteps: 6,
  tools: [graphAddTool(v3)],
});
const setupRun = await setup.run(
  'Call graph_add once with these quads (source "user:17", keep quotes): ' +
  '1. ex:Initech rdf:type ex:Company ; 2. ex:Initech ex:emp_name "Initech" ; ' +
  '3. ex:Initech ex:office "Basement" . Reply with just: stored',
);
console.log("SETUP:", setupRun.text.trim().slice(0, 30));

const before = await reviewOntology(v3);
console.log("v3 TERMS:", [...before.classes, ...before.properties].map((t) => `${t.term}(${t.uses})`));

// The evolution decision: standardize emp_name -> name, deprecate office ->
// location, tighten Company shapes. Real batteries check both sides.
const battery = [
  { id: "companies-exist", ask: "SELECT ?c WHERE { ?c rdf:type ex:Company . }" },
  {
    id: "named-companies",
    shape: { targetClass: "ex:Company", properties: [{ path: "ex:name", minCount: 1 }] },
  },
];

// Fails BEFORE the rewrite (nothing uses ex:name yet) — so run it after.
const migration = await migrateTBox(
  v3,
  {
    from: "v3",
    to: "v4",
    renames: [{ from: "ex:emp_name", to: "ex:name" }],
    deprecations: [{ term: "ex:office", replacement: "ex:location", reason: "schema alignment" }],
    added: [
      { subject: "ex:name", predicate: "rdfs:range", object: "xsd:string", source: "user:17" },
      { subject: "ex:name", predicate: "rdfs:domain", object: "ex:Company", source: "user:17" },
    ],
    shapes: [{ targetClass: "ex:Company", properties: [{ path: "ex:name", minCount: 1 }] }],
  },
  { battery: [{ id: "companies-exist", ask: "SELECT ?c WHERE { ?c rdf:type ex:Company . }" }] },
);
console.log("MIGRATION:", { status: migration.report.status, renamed: migration.report.rewrittenQuads, tboxGraph: migration.report.tboxGraph });
if (!migration.store) throw new Error(`evolution failed: ${migration.report.errors.join("; ")}`);

// Post-evolution conformance v4 must pass BOTH checks now.
const post = await runConformance(migration.store, battery);
console.log("v4 CONFORMANCE:", post.passed, post.results.map((r) => `${r.id}:${r.passed}`));

// v3 store untouched — both versions coexist; the v4 TBox is its own named graph.
const v4Tbox = await migration.store.match(null, null, null, migration.report.tboxGraph!);
console.log("v4 TBOX GRAPH QUADS:", v4Tbox.length, "| v3 old-predicate still there:", (await v3.match(null, "ex:emp_name", null)).length === 1);
const jsonld = quadsToJsonLd(await migration.store.match(null, null, null));
console.log("JSON-LD NAMED GRAPHS:", (jsonld["@graph"] as Record<string, unknown>[]).map((n) => (Array.isArray(n["@graph"]) ? String(n["@id"]) : "default")));
console.log(post.passed && v4Tbox.length > 0 ? "PASS: ontology evolved v3->v4 with battery + named TBox graph" : "NOTE: inspect report");