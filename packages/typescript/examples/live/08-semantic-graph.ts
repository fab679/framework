// LIVE: semantic layer with a real model — validated writes via graph_add,
// claim_check before answering, entity linking, ontology self-review + SHACL induction.
import {
  Agent, loadConfig, providerFromEnv, InMemoryGraphStore,
  graphAddTool, claimCheckTool, sparqlSelectTool, ontologyLookupTool,
  EntityLinker, linkEntityTool, proposeMappingTool, MappingProposals,
  reviewOntology, reviewOntologyTool, proposeShapes, proposeShapesTool, adoptShapes,
  modelRetryMiddleware,
} from "../../src/index.js";

const cfg = loadConfig();
const store = new InMemoryGraphStore({
  shapes: [{ targetClass: "ex:Company", properties: [{ path: "ex:name", minCount: 1 }] }],
});
const linker = new EntityLinker(store);
const proposals = new MappingProposals({ store }); // autonomy default: commits on propose
const agent = new Agent({
  name: "graph-keeper",
  model: providerFromEnv(cfg.active, { maxTokens: 1200 }),
  middleware: [modelRetryMiddleware({ maxRetries: 3, initialDelayMs: 500 })], // transient network errors
  maxSteps: 8,
  tools: [
    graphAddTool(store),
    claimCheckTool(store),
    sparqlSelectTool(store),
    ontologyLookupTool(store),
    linkEntityTool(linker),
    proposeMappingTool(proposals),
    reviewOntologyTool(store),
    proposeShapesTool(store, { minInstances: 1, coverage: 0.5 }),
  ],
});

// 1) The model commits validated, provenance-carrying facts. Exact quads are
// spelled out deterministically so a live model just has to carry them.
const write = await agent.run(
  "Call the graph_add tool exactly once with these 6 quads " +
  '(source "user:demo" for every one, object values keep the quotes shown):\n' +
  '1. subject ex:GlobalCorp, predicate rdf:type, object ex:Company\n' +
  '2. subject ex:GlobalCorp, predicate ex:name, object "Global Corp"\n' +
  '3. subject ex:GlobalCorp, predicate ex:headcount, object 42\n' +
  '4. subject ex:Acme, predicate rdf:type, object ex:Company\n' +
  '5. subject ex:Acme, predicate ex:name, object "Acme"\n' +
  '6. subject ex:Acme, predicate ex:headcount, object 10\n' +
  'If validation fails, fix the reported issue and retry. Then reply with just: stored',
);
console.log("WRITE RESULT:", write.text.trim());

// 2) Ontology self-review: what did we end up with, what's missing?
const review = await reviewOntology(store);
console.log("TERMS:", { classes: review.classes.map((c) => `${c.term}(${c.uses})`), properties: review.properties.map((p) => `${p.term}(${p.uses})`) });
console.log("UNDECLARED (what to add next):", review.undeclared.map((t) => t.term));
console.log("MISSING domain/range:", { domain: review.missingDomain, range: review.missingRange });

// 3) SHACL induction from the model's OWN data + builder-side adoption.
const shapes = await proposeShapes(store, { minInstances: 1, coverage: 0.5 });
console.log("PROPOSED SHAPES:", JSON.stringify(shapes));
const { adopted, rejected } = await adoptShapes(store, shapes);
console.log("ADOPTED:", adopted.length, "REJECTED:", rejected.length, rejected.map((r) => r.violations));

// 4) Entity linking + an autonomous mapping commit.
const linkedAda = await linker.link("Global Corp");
console.log("LINK 'Global Corp':", linkedAda.map((l) => ({ iri: l.iri, score: l.score })));
const mapped = await proposals.propose("globalcorporation", linkedAda[0]?.iri ?? "ex:GlobalCorp", "user:demo");
console.log("MAPPING (autonomy default):", mapped.status, "— linker now resolves the alias:", (await linker.link("globalcorporation")).length > 0);

// 5) A closure query + provenance proof.
const rows = await store.match(null, "ex:name", null);
console.log("STORE HAS", rows.length, "named entities; closure:", (await store.match(null, null, null)).length, "quads");

// 6) A second, fresh agent can ground answers against the compounding graph.
const reader = new Agent({
  name: "reader",
  model: providerFromEnv(cfg.active, { maxTokens: 300 }),
  middleware: [modelRetryMiddleware({ maxRetries: 3, initialDelayMs: 500 })], // transient network errors
  maxSteps: 4,
  tools: [claimCheckTool(store), sparqlSelectTool(store)],
});
const answer = await reader.run('How many employees does Global Corp have? Use claim_check (subject "ex:GlobalCorp", predicate "ex:headcount") or sparql_select to ground your answer.');
console.log("GROUNDED ANSWER:", answer.text.trim());