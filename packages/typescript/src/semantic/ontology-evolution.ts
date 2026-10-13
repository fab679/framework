import type { NodeShape, Quad, StoreOptions } from "./index.js";
import { InMemoryGraphStore, executeSelect } from "./index.js";

/**
 * Ontology evolution (roadmap P2-sem, docs/07 lifecycle stage "evolve"):
 * TBox v3 -> v4 with a migration that is checked before AND after by a
 * conformance battery, and that never destroys the old store.
 *
 * The store has no delete by design; migration therefore REBUILDS: explicit
 * facts are exported from the old store, transformed (axiom add/remove,
 * term renames, deprecations), re-validated and re-entailed into a fresh
 * v4 store. The v3 store survives untouched — rollback is "don't adopt".
 */

export interface ConformanceCheck {
  id: string;
  /**
   * SPARQL-subset SELECT (see executeSelect). Passes when the row count is
   * within [minRows, maxRows]. Default: at least one row (ask-style);
   * `minRows: 0, maxRows: 0` asserts absence.
   */
  ask?: string;
  minRows?: number;
  maxRows?: number;
  /** An extra SHACL shape the closure must satisfy. */
  shape?: NodeShape;
  /** Why this check matters — surfaces in failure reports. */
  reason?: string;
}

export interface ConformanceResult {
  id: string;
  passed: boolean;
  detail: string;
}

export interface ConformanceReport {
  passed: boolean;
  results: ConformanceResult[];
}

/** Run a battery of checks against a store's closure. */
export async function runConformance(
  store: InMemoryGraphStore,
  battery: ConformanceCheck[],
): Promise<ConformanceReport> {
  const results: ConformanceResult[] = [];
  const runner = async (check: ConformanceCheck, closure: Quad[]): Promise<void> => {
    if (check.ask) {
      const rows = await executeSelect(store, check.ask);
      const min = check.minRows ?? 1;
      const ok = rows.length >= min && (check.maxRows == null || rows.length <= check.maxRows);
      results.push({
        id: check.id,
        passed: ok,
        detail: `${rows.length} rows (expected >= ${min}${check.maxRows == null ? "" : `, <= ${check.maxRows}`})`,
      });
      return;
    }
    if (check.shape) {
      // Battery shapes aren't store-registered: validate with a scratch
      // store carrying ONLY this shape (checkShapes is pure — no entailment).
      const scratch = new InMemoryGraphStore({ shapes: [check.shape] });
      const mine = scratch.checkShapes(closure).violations;
      results.push({
        id: check.id,
        passed: mine.length === 0,
        detail: mine.length ? mine.map((v) => v.message).join("; ") : "shape holds",
      });
      return;
    }
    results.push({
        id: check.id,
        passed: false,
        detail: `check has neither ask nor shape${check.reason ? ` (${check.reason})` : ""}`,
      });
  };
  const closure = await store.match(null, null, null);
  for (const check of battery) {
    try {
      await runner(check, closure);
    } catch (err) {
      results.push({ id: check.id, passed: false, detail: err instanceof Error ? err.message : String(err) });
    }
  }
  return { passed: results.every((r) => r.passed), results };
}

export interface TBoxMigrationSpec {
  from: string;
  to: string;
  /** New TBox axioms to add (provenance required by the store). */
  added?: Quad[];
  /** Exact axioms to retract (matched on subject|predicate|object). */
  removed?: { subject: string; predicate: string; object: string }[];
  /**
   * Term renames applied to BOTH the TBox and ABox in all three positions
   * (subject/predicate/object): class splits, predicate renames, IRI moves.
   */
  renames?: { from: string; to: string }[];
  /** Terms deprecated in this version; with `replacement`, uses are rewritten. */
  deprecations?: { term: string; replacement?: string; reason: string }[];
  /** Shapes/rules carried by the NEW store (defaults to the old store's). */
  shapes?: NodeShape[];
  rules?: StoreOptions["rules"];
}

export interface MigrationReport {
  from: string;
  to: string;
  status: "migrated" | "aborted" | "failed";
  addedQuads: number;
  removedQuads: number;
  rewrittenQuads: number;
  /** Named graph the v4 TBox axioms were written to (match(.., ..., tboxGraph)). */
  tboxGraph?: string;
  deprecatedWarnings: string[];
  errors: string[];
  preBattery?: ConformanceReport;
  postBattery?: ConformanceReport;
}

export interface MigrationResult {
  report: MigrationReport;
  /** Present on success — the new-version store with a fresh closure. The v3 store is untouched. */
  store?: InMemoryGraphStore;
}

function termKey(q: { subject: string; predicate: string; object: string }): string {
  return `${q.subject}|${q.predicate}|${q.object}`;
}

/** Ontology-defining predicates: shared with the review auditor. */
export const TBOX_PREDICATES = new Set([
  "rdfs:subClassOf",
  "http://www.w3.org/2000/01/rdf-schema#subClassOf",
  "rdfs:subPropertyOf",
  "http://www.w3.org/2000/01/rdf-schema#subPropertyOf",
  "rdfs:domain",
  "http://www.w3.org/2000/01/rdf-schema#domain",
  "rdfs:range",
  "http://www.w3.org/2000/01/rdf-schema#range",
  "owl:inverseOf",
  "http://www.w3.org/2002/07/owl#inverseOf",
]);

/** rdf:type objects that mark a TERM declaration, not an instance fact. */
export const OWL_VOCAB = new Set([
  "owl:TransitiveProperty",
  "http://www.w3.org/2002/07/owl#TransitiveProperty",
  "owl:SymmetricProperty",
  "http://www.w3.org/2002/07/owl#SymmetricProperty",
  "owl:FunctionalProperty",
  "http://www.w3.org/2002/07/owl#FunctionalProperty",
  "owl:Class",
  "http://www.w3.org/2002/07/owl#Class",
  "rdfs:Class",
  "http://www.w3.org/2000/01/rdf-schema#Class",
  "rdfs:Datatype",
  "http://www.w3.org/2000/01/rdf-schema#Datatype",
]);

/**
 * Migrate a TBox. Order: pre-battery (abort on failure) -> rebuild ->
 * post-battery (report failure; caller decides whether to adopt).
 */
export async function migrateTBox(
  oldStore: InMemoryGraphStore,
  spec: TBoxMigrationSpec,
  opts: { battery?: ConformanceCheck[]; factory?: (opts: StoreOptions) => InMemoryGraphStore; tboxGraph?: string | false } = {},
): Promise<MigrationResult> {
  const report: MigrationReport = {
    from: spec.from,
    to: spec.to,
    status: "migrated",
    addedQuads: 0,
    removedQuads: 0,
    rewrittenQuads: 0,
    deprecatedWarnings: [],
    errors: [],
  };

  if (opts.battery?.length) {
    report.preBattery = await runConformance(oldStore, opts.battery);
    if (!report.preBattery.passed) {
      report.status = "aborted";
      report.errors.push("pre-migration conformance battery failed — ontology not migrated");
      return { report };
    }
  }

  const removeKeys = new Set((spec.removed ?? []).map(termKey));
  const renameMap = new Map<string, string>();
  for (const r of spec.renames ?? []) renameMap.set(r.from, r.to);
  for (const d of spec.deprecations ?? []) {
    if (d.replacement) {
      renameMap.set(d.term, d.replacement);
      report.deprecatedWarnings.push(`${d.term} deprecated (${d.reason}) — rewritten to ${d.replacement}`);
    } else {
      report.deprecatedWarnings.push(`${d.term} deprecated (${d.reason}) — uses kept, flagged`);
    }
  }

  const rewriteTerm = (term: string): string => renameMap.get(term) ?? term;

  const factory = opts.factory ?? ((o: StoreOptions) => new InMemoryGraphStore(o));
  const newStore = factory({
    shapes: spec.shapes ?? oldStore.ontologyShapes(),
    rules: spec.rules ?? oldStore.ontologyRules(),
  });

  const explicit = (await oldStore.match(null, null, null)).filter((q) => q.source !== "entailment");
  // Named-graph use: the v4 ontology is its OWN dataset inside the store —
  // every version stays addressable (match(null, null, null, tboxGraph)),
  // JSON-LD round-trips it as a named graph, and `false` opts out.
  const tboxGraph = opts.tboxGraph === false ? undefined : opts.tboxGraph ?? `urn:framework:ontology:${spec.to}`;
  report.tboxGraph = tboxGraph;
  const isAxiom = (q: Quad, predicatesInUse: Set<string>): boolean => {
    if (TBOX_PREDICATES.has(q.predicate)) return true;
    // rdf:type is TBox when it types a TERM (property/class used somewhere)
    // or carries an OWL vocabulary marker — it's ABox for plain instances
    // (prefixed and full-IRI forms both count).
    if (q.predicate === "rdf:type" || q.predicate === "a" || q.predicate === "http://www.w3.org/1999/02/22-rdf-syntax-ns#type") {
      return predicatesInUse.has(q.subject) || OWL_VOCAB.has(q.object);
    }
    return false;
  };
  const predicatesInUse = new Set(explicit.filter((q) => !removeKeys.has(termKey(q))).map((q) => rewriteTerm(q.predicate)));

  const rewrittenSpecAdded = (spec.added ?? []).map((aq) =>
    tboxGraph ? { ...aq, graph: tboxGraph, derivedFrom: `migration:${spec.from}->${spec.to}` } : { ...aq, derivedFrom: `migration:${spec.from}->${spec.to}` },
  );
  const rebuilt: Quad[] = [];
  for (const q of explicit) {
    if (removeKeys.has(termKey(q))) {
      report.removedQuads += 1;
      continue;
    }
    const next: Quad = {
      subject: rewriteTerm(q.subject),
      predicate: rewriteTerm(q.predicate),
      object: rewriteTerm(q.object),
      source: q.source,
      ...(q.graph ? { graph: q.graph } : {}),
      ...(q.derivedFrom ? { derivedFrom: q.derivedFrom } : {}),
    };
    if (isAxiom(next, predicatesInUse) && tboxGraph) next.graph = tboxGraph;
    if (termKey(next) !== termKey(q)) {
      report.rewrittenQuads += 1;
      next.derivedFrom = `${next.derivedFrom ? `${next.derivedFrom};` : ""}migration:${spec.from}->${spec.to}`;
    }
    rebuilt.push(next);
  }
  for (const aq of rewrittenSpecAdded) rebuilt.push(aq);
  report.addedQuads = (spec.added ?? []).length;

  try {
    await newStore.add(rebuilt);
  } catch (err) {
    report.status = "failed";
    report.errors.push(`v4 store rejected migrated facts: ${err instanceof Error ? err.message : String(err)}`);
    return { report };
  }

  if (opts.battery?.length) {
    report.postBattery = await runConformance(newStore, opts.battery);
    if (!report.postBattery.passed) {
      report.status = "failed";
      const failed = report.postBattery.results.filter((r) => !r.passed).map((r) => `${r.id}: ${r.detail}`);
      report.errors.push(`post-migration conformance battery failed — ${failed.join("; ")}`);
      return { report };
    }
  }

  return { report, store: newStore };
}