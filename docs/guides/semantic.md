# Semantic graph (P2-sem)

The graph layer is the agent's **ground truth layer**: what it may claim, what it remembers across sessions, what proves its answers. The framework ships the machinery (registry → lookup → validation → versioned evolution); content is always user-supplied. Design background: [design docs](../design/index.md) (07, 08).

## GraphStore + provenance rules

`GraphStore` contract (both languages): `add(quads)`, `match(s?, p?, o?, g?)` (null/undefined wildcard; queries the entailment **closure**; a `graph` value selects one named graph — identity is subject|predicate|object|graph), `validate(quads)` → `ShapeReport`, and `why(quad)` — the proof chain `{ premises, rule }` for an entailed fact, `null` for explicit ones.

`Quad { subject, predicate, object, graph?, source?, derivedFrom?, rule? }`. **Provenance is mandatory on explicit writes** — `validate()` flags missing `source`, so `add()` rejects unattributed facts. Functional-property conflicts and SHACL violations roll the whole `add()` back: a conflict is never committed as a second fact.

`InMemoryGraphStore({ shapes, rules })` computes entailment **to fixpoint** over an RDFS subset + OWL property axioms: `RDFS-SUB-TRANS`, `RDFS-TYPE`, `RDFS-SUBPROP-TRANS`, `RDFS-SUBPROP`, `RDFS-DOMAIN`, `RDFS-RANGE`, `OWL-TRANSITIVE`, `OWL-SYMMETRIC`, `OWL-INVERSE` (both directions), plus versioned CONSTRUCT-style `GraphRule`s tagged `<ruleId>@<version>` in `why()` proofs. SHACL-subset `NodeShape { targetClass, properties: PropertyShape[] }` supports `minCount`/`maxCount`/`datatype`/`nodeKind iri|literal`/`hasValue`.

## Agent tools

Same tool names in both languages (factory functions: `graphAddTool`/`graph_add_tool`, `claimCheckTool`/`claim_check_tool`, ...) — wire them like any tools:

```ts
import { InMemoryGraphStore, graphAddTool, claimCheckTool, sparqlSelectTool } from "framework-agents";

const store = new InMemoryGraphStore({ shapes: [...] });
new Agent({
  model,
  tools: [graphAddTool(store), claimCheckTool(store), sparqlSelectTool(store)],
});
```

- **`graph_add`** — the autonomous validated write. SHACL + provenance + functional checks run inside the store; **violations return as tool results, not throws** (`"Validation failed — fix and retry: ..."`) so the model repairs and retries itself. On success it reports `{ committed, inferred }`. Args: `{ quads: [{subject, predicate, object, source}] }` (1–50; `source` is described as the required provenance URI on the schema itself).
- **`claim_check`** — anti-hallucination primitive. Verdict before asserting: `grounded` (in the closure, with proof), `unknown` (absent — do not assert), or `contradicted` (clashes with a functional property's existing value, with the existing values as proof).
- **`sparql_select`** — read-only SPARQL-subset SELECT (`SELECT ?a ?b WHERE { s p o . ... }`) over the inferred closure; anything outside the subset throws (a documented subset, not full SPARQL).
- **`ontology_lookup`** — find classes/properties/labels matching a term, so models stop inventing predicates.
- **`link_entity`** — mention → canonical IRIs (below).

## EntityLinker + MappingProposals

`EntityLinker(store)` — a case-insensitive label index (`rdfs:label`, `skos:prefLabel`, `skos:altLabel` and their full-IRI forms) over the closure, canonicalized through `owl:sameAs` union-find groups: a mention linked through any alias resolves to **one representative IRI**. `link(mention, k = 5)` returns `LinkedEntity { iri, label?, score, sameAsGroup }`; `linkEntityTool(linker)` exposes it as the `link_entity` tool ("never invent IRIs").

**Autonomy default.** `MappingProposals` — the LLM maps a raw term to a canonical IRI via the `propose_mapping` tool. Autonomy is **on by default**: with a bound store, a proposal commits **immediately** as a provenance-carrying `skos:altLabel` the linker sees forever (matching the autonomous `graph_add` path). Opt out with `autonomy: false` (`MappingProposals(store=..., autonomy=False)`), which queues proposals as `pending` for a human `decide(store, id, "approved"|"rejected")`, or gate the tool with [HITL middleware](middleware.md). Provenance is mandatory either way; commit failures come back as repair feedback, same contract as `graph_add`. Options also allow review mode with no store bound (proposals queue).

## JSON-LD round trip

`quadsToJsonLd(quads, prefixes?)` / `quads_to_json_ld` and `jsonldToQuads(doc, prefixes?)` / `json_ld_to_quads` expand/compact against a standard prefix map (`rdf`, `rdfs`, `owl`, `xsd`, `skos`, `dct`, `prov`, `schema`). **Named graphs round-trip**: quads carrying a `graph` become `{"@id": graph, "@graph": [...]}` entries, graph-less quads land in the default graph, and importing preserves them. Provenance fields (`source`, `rule`) do **not** round-trip — JSON-LD carries the statements, the store keeps the evidence.

## Ontology self-review

Audit what the ontology **is**, not what it wishes it were (both languages):

- **`reviewOntology(store)`** / `review_ontology` — inventory + gap list, pure read: classes/properties with use + axiom counts, `undeclared` terms (used in data, zero TBox declarations), `unused`, `unlabeled`, properties `missingDomain`/`missingRange`, and explicit-vs-closure quad counts. Agent tools: `reviewOntologyTool` / `review_ontology_tool`.
- **`proposeShapes(...)`** — SHACL **induction** from instance data: observes each class's instances and emits candidate `NodeShape`s (count bounds, datatype, nodeKind) that explain the observed data. Agent tool: `proposeShapesTool`.
- **`adoptShapes(store, shapes)`** / `adopt_shapes` — **the builder owns adoption, never a silent self-adoption**: proposals are adopted explicitly (adopting validates the existing closure first; the `ShapeAdoption` result reports accepted and rejected shapes), or carried into the next migration via `shapes` on the spec.

## Migration: migrateTBox

`migrateTBox(oldStore, spec, opts)` / `migrate_t_box` — TBox v3 → v4 with a checked migration that never destroys the old store:

- `TBoxMigrationSpec { from, to, added?, removed?, renames?, deprecations?, shapes?, rules? }` — axiom adds (provenance required), exact retractions (matched on subject|predicate|object), term renames applied in all three positions across TBox+ABox, and deprecations (with `replacement` = rewrite, without = flag-only).
- `opts.battery: ConformanceCheck[]` — a conformance battery of `ask` SPARQL-subset SELECTs with row-count ranges (`minRows: 0, maxRows: 0` asserts absence; default at least one row) and extra SHACL shapes, with `reason` strings for failure reports. The battery runs **before** (failure → `status: "aborted"`, nothing migrated) and **after** (failure → `status: "failed"`, caller decides whether to adopt).
- The store has no delete by design; migration therefore **rebuilds**: explicit facts are exported, transformed, re-validated, and re-entailed into a fresh store. The old store survives untouched — rollback is "don't adopt".
- Axioms are written to a **version-named graph** so every ontology version stays addressable (`match(null, null, null, tboxGraph)`), and the JSON-LD round trip exports it as a named graph. Python names it **`urn:framework:ontology:<to>`**; the graph name is configurable in both ports (`tboxGraph` / `tbox_graph`, `false` opts out of graphing entirely).
- `MigrationResult { report, store? }` — the report counts added/removed/rewritten quads, carries deprecation warnings and errors plus both battery reports (`preBattery`/`postBattery`); `store` is present only on success.

## Remote SPARQL

For data that lives outside the agent's own graph, `RemoteSparqlAdapter` / `RemoteSparqlAdapter` talks to real endpoints (Wikidata Query Service, Fuseki, GraphDB, Virtuoso):

- Protocol: SPARQL 1.1 — SELECT via `POST` with content-type `application/sparql-query` and `Accept: application/sparql-results+json`; UPDATE (INSERT/DELETE...) posts `application/sparql-update` (`update()`, opt-in). Rows come back as plain-string term records, the same shape as the local `executeSelect`.
- **Default descriptive User-Agent** (`agent-framework/0.1 (+RemoteSparqlAdapter)`) — public endpoints (Wikidata notably) reject plain clients with 403.
- **Builtin retries** (default 2) for transient failures: 429/5xx/network, honoring **`Retry-After`** when the server sends it (capped at 60s; otherwise exponential backoff). Model-call retry middleware is a separate layer.
- Auth: `basic` (user/password), `bearer` (token), or a custom `header` (name/value); pinned `prefixes` are prepended as `PREFIX` declarations so queries stay short; per-request timeout 30s; `updateEndpoint` defaults to `endpoint`.
- `remoteSparqlTool(adapter, { allowUpdate })`: exposes read-only `remote_sparql_select` by default; pass `allowUpdate: true` to expose `remote_sparql` with an `{ update: true }` flag per call. Never wire UPDATE into a read-only agent.

## GraphMemory

Session memory + long-term store in one — see [memory & sessions](memory-sessions.md). `load()` injects the top-`facts` (default 8) closure entries relevant to the latest user turn as a grounding assistant message; `save()` delegates to inner memory (extraction stays model-driven via `graph_add`); state round-trips through `exportState()`/`importState()`.

## Related

- [Design docs 07–08](../07-semantics-governance.md) — governance layers and the usage playbook (the source of these semantics)
- [Memory & sessions](memory-sessions.md) — the GraphMemory wiring