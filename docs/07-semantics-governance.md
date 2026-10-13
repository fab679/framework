# 07 — Semantics Governance (who owns meaning)

## Principle
The framework never ships "the ontology." It ships the **machinery for governing meaning**:
registry → lookup → validation → versioned evolution. Content is always user-supplied.

## Ownership layers
| Layer | What | Owner | Lives in |
|---|---|---|---|
| Foundation vocab | `schema.org`, FOAF, DCTerms, PROV-O, SKOS | Standards bodies | `src/semantic/vocab.ts` (prefixes only) |
| Domain ontology (TBox) | Classes, properties, ranges — the stable meaning | Data steward / domain expert | Versioned files, e.g. `ontologies/core@v3.ttl` |
| Application shapes | Per-agent constraints (required fields, ranges) | Agent builder | SHACL `shapes/<agent>.ttl`, versioned with the agent |

## Rules of the road
1. **Agents look up, never invent.** Unknown predicate → SHACL gate rejects + suggests canonical term. No silent drift (`worksAt` vs `worksFor`).
2. **LLM proposes mappings; autonomy by default, review optional.** `emp_location → schema:workLocation` commits on proposal (`new MappingProposals({ store })` — default), matching the autonomous write path everywhere else; provenance (`dct:source`) is mandatory. Where drift is unacceptable, opt into human review: `new MappingProposals({ autonomy: false })` queues proposals for `decide()`, or gate the tool with humanInTheLoopMiddleware.
3. **TBox stable, ABox streams.** Ontology changes on release cadence with review; facts stream continuously. Queries/rules/dashboards couple to the TBox, never to source formatting.
4. **Provenance mandatory on writes.** Every agent-committed quad carries `dct:source` + `prov:wasDerivedFrom` (+ `validUntil` where applicable). No source = rejected at `GraphStore.validate`. Claims are queryable objects with evidence, not footnotes.
5. **Text is the interface, triples are the asset.** Runs return text but commit validated triples — the graph compounds, prompts don't have to.

## Lifecycle
`bootstrap (reuse foundation vocab)` → `map (LLM proposes source→ontology)` → `approve (human)` → `enforce (SHACL on every write)` → `evolve (TBox v3→v4 with migration + conformance tests)`.
