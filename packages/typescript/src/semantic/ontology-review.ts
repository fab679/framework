import { z } from "zod";
import { defineTool } from "../tools/index.js";
import type { NodeShape, Quad } from "./index.js";
import { InMemoryGraphStore } from "./index.js";
import { LABEL_PREDICATES } from "./entity-linking.js";
import { TBOX_PREDICATES, OWL_VOCAB } from "./ontology-evolution.js";

/**
 * Ontology self-review (extends docs/07 lifecycle: enforce -> evolve):
 * the agent audits what its own ontology IS — not what it wishes it were —
 * directly from the store, and identifies what to add:
 *
 * 1. reviewOntology(): inventory + gaps. Used-but-undeclared terms,
 *    declared-but-unused terms, unlabeled terms, properties missing
 *    rdfs:domain/range. "Where is X used?", "what's missing?" — one call.
 * 2. proposeShapes(): SHACL INDUCTION from instance data — observe each
 *    class's instances, emit candidate NodeShapes (minCount/maxCount,
 *    datatype, nodeKind) that explain the observed data. Per docs/07 the
 *    builder owns shapes: these are PROPOSALS — adopt via adoptShapes()
 *    (validates the existing closure first) or the next
 *    migrateTBox({ shapes }); never a silent self-adoption.
 */

const RDF_TYPE = new Set(["rdf:type", "a", "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"]);

export interface TermStat {
  term: string;
  kind: "class" | "property";
  /** Explicit ABox uses: instance-of counts for classes, fact counts for properties. */
  uses: number;
  /** TBox axioms mentioning the term (declarations, subClassOf, domain/range, ...). */
  axiomCount: number;
  label?: string;
}

export interface OntologyReview {
  classes: TermStat[];
  properties: TermStat[];
  /** Used in data with zero TBox declarations — the "what to add" list. */
  undeclared: TermStat[];
  /** Declared but never used in any explicit fact. */
  unused: string[];
  /** Terms without rdfs:label / skos:prefLabel / altLabel. */
  unlabeled: string[];
  /** Properties missing rdfs:domain / rdfs:range axioms. */
  missingDomain: string[];
  missingRange: string[];
  /** Explicit quads vs closure size — how much the ontology explains. */
  explicitQuads: number;
  closureQuads: number;
}

/** True when the quad is an ontology statement, not a fact. */
const isAxiomQuad = (q: Quad, predicatesInUse: Set<string>): boolean =>
  TBOX_PREDICATES.has(q.predicate) ||
  (RDF_TYPE.has(q.predicate) && (predicatesInUse.has(q.subject) || OWL_VOCAB.has(q.object)));

function strip(term: string): string {
  const m = term.match(/^"(.*)"(\^\^<?[\w:/#.-]+>?|@[\w-]+)?$/);
  return m ? m[1] : term;
}

/** Audit the store: inventory + gap list. Pure read — never mutates. */
export async function reviewOntology(store: InMemoryGraphStore): Promise<OntologyReview> {
  const closure = await store.match(null, null, null);
  const explicit = closure.filter((q) => q.source !== "entailment");
  const predicatesInUse = new Set(explicit.map((q) => q.predicate));

  const labels = new Map<string, string>();
  for (const q of closure) {
    if (LABEL_PREDICATES.has(q.predicate)) labels.set(q.subject, strip(q.object));
  }

  const classes = new Map<string, TermStat>();
  const properties = new Map<string, TermStat>();
  const class_ = (term: string): TermStat => {
    let s = classes.get(term);
    if (!s) {
      s = { term, kind: "class", uses: 0, axiomCount: 0, label: labels.get(term) };
      classes.set(term, s);
    }
    return s;
  };
  const property = (term: string): TermStat => {
    let s = properties.get(term);
    if (!s) {
      s = { term, kind: "property", uses: 0, axiomCount: 0, label: labels.get(term) };
      properties.set(term, s);
    }
    return s;
  };

  const domains = new Map<string, number>();
  const ranges = new Map<string, number>();
  for (const q of explicit) {
    const axiom = isAxiomQuad(q, predicatesInUse);

    // rdf:type: axiom = term declaration (TBox) ; fact = instance typing (ABox).
    if (RDF_TYPE.has(q.predicate)) {
      if (axiom) class_(q.subject).axiomCount += 1;
      else class_(q.object).uses += 1;
      continue;
    }

    if (TBOX_PREDICATES.has(q.predicate)) {
      // TBox statements introduce/declare their subject term.
      const subject = q.predicate.includes("subClassOf") ? class_(q.subject) : property(q.subject);
      subject.axiomCount += 1;
      // Their object is a class term (domain/range/subClassOf targets).
      if (!q.predicate.includes("inverseOf")) class_(q.object).axiomCount += 1;
      if (q.predicate.includes("domain")) domains.set(q.subject, (domains.get(q.subject) ?? 0) + 1);
      if (q.predicate.includes("range")) ranges.set(q.subject, (ranges.get(q.subject) ?? 0) + 1);
      continue;
    }

    // ABox fact: a property use with actual data behind it.
    property(q.predicate).uses += 1;
  }
  // owl:inverseOf subjects/objects are property terms too (handled below via
  // the property map only when they carry any axiom or use).

  const all = [...classes.values(), ...properties.values()];
  const undeclared = all.filter((t) => t.uses > 0 && t.axiomCount === 0);
  const unused = all.filter((t) => t.axiomCount > 0 && t.uses === 0).map((t) => t.term);
  const unlabeled = all.filter((t) => !t.label).map((t) => t.term);
  const missingDomain = [...properties.values()]
    .filter((t) => t.axiomCount > 0 || t.uses > 0) // known terms only; no vocab noise
    .filter((t) => !domains.has(t.term))
    .map((t) => t.term);
  const missingRange = [...properties.values()]
    .filter((t) => t.axiomCount > 0 || t.uses > 0)
    .filter((t) => !ranges.has(t.term))
    .map((t) => t.term);

  return {
    classes: [...classes.values()].sort((a, b) => b.uses - a.uses || a.term.localeCompare(b.term)),
    properties: [...properties.values()].sort((a, b) => b.uses - a.uses || a.term.localeCompare(b.term)),
    undeclared: undeclared.sort((a, b) => b.uses - a.uses),
    unused,
    unlabeled,
    missingDomain,
    missingRange,
    explicitQuads: explicit.length,
    closureQuads: closure.length,
  };
}

// ---------------------------------------------------------------------------
// SHACL induction from instance data
// ---------------------------------------------------------------------------

export interface ShapeInductionOptions {
  /** Classes with fewer instances produce no shape. Default 2. */
  minInstances?: number;
  /** Only emit a property shape when >= this fraction of instances have it. Default 0.5. */
  coverage?: number;
}

/**
 * Propose NodeShapes by observing explicit instance data: for each class,
 * per-property coverage, cardinality, datatypes, node kinds across its
 * instances. PROPOSAL ONLY per docs/07 (shapes are builder-owned).
 */
export async function proposeShapes(
  store: InMemoryGraphStore,
  opts: ShapeInductionOptions = {},
): Promise<NodeShape[]> {
  const minInstances = opts.minInstances ?? 2;
  const coverage = opts.coverage ?? 0.5;
  const explicit = (await store.match(null, null, null)).filter((q) => q.source !== "entailment");
  const predicatesInUse = new Set(explicit.map((q) => q.predicate));

  // Class -> instance subjects, from EXPLICIT type facts only (closure types
  // would smear subclass properties up into parent shapes).
  const instancesOf = new Map<string, Set<string>>();
  for (const q of explicit) {
    if (!RDF_TYPE.has(q.predicate) || isAxiomQuad(q, predicatesInUse)) continue;
    const set = instancesOf.get(q.object) ?? new Set<string>();
    set.add(q.subject);
    instancesOf.set(q.object, set);
  }

  const shapes: NodeShape[] = [];
  for (const [cls, subjects] of instancesOf) {
    if (subjects.size < minInstances) continue;
    // Per-property observation across this class's instances.
    const observed = new Map<
      string,
      { present: number; maxSeen: number; datatypes: Set<string>; nodeKinds: Set<"iri" | "literal"> }
    >();
    const bySubject = new Map<string, Quad[]>();
    for (const q of explicit) {
      if (!subjects.has(q.subject)) continue;
      bySubject.set(q.subject, [...(bySubject.get(q.subject) ?? []), q]);
    }
    for (const [, quads] of bySubject) {
      const byPath = new Map<string, Quad[]>();
      for (const q of quads) {
        if (RDF_TYPE.has(q.predicate)) continue;
        byPath.set(q.predicate, [...(byPath.get(q.predicate) ?? []), q]);
      }
      for (const [path, pathQuads] of byPath) {
        const o = observed.get(path) ?? { present: 0, maxSeen: 0, datatypes: new Set<string>(), nodeKinds: new Set<"iri" | "literal">() };
        o.present += 1;
        o.maxSeen = Math.max(o.maxSeen, pathQuads.length);
        for (const q of pathQuads) {
          o.nodeKinds.add(isLiteralTerm(q.object) ? "literal" : "iri");
          const dt = datatypeOf(q.object);
          if (dt) o.datatypes.add(dt);
        }
        observed.set(path, o);
      }
    }
    const properties: NodeShape["properties"] = [];
    for (const [path, o] of observed) {
      if (o.present / subjects.size < coverage) continue;
      const shape: NodeShape["properties"][number] = { path };
      if (o.present === subjects.size) shape.minCount = 1;
      if (o.maxSeen === 1) shape.maxCount = 1;
      if (o.datatypes.size === 1) shape.datatype = [...o.datatypes][0];
      if (o.nodeKinds.size === 1) shape.nodeKind = [...o.nodeKinds][0];
      properties.push(shape);
    }
    if (properties.length) shapes.push({ targetClass: cls, properties });
  }
  return shapes;
}

function isLiteralTerm(term: string): boolean {
  if (term.startsWith('"')) return true;
  if (/^(-?\d+(\.\d+)?|true|false)$/.test(term)) return true;
  if (term.startsWith("<") || /^[a-zA-Z][\w-]*:/.test(term)) return false;
  return true;
}

function datatypeOf(term: string): string | undefined {
  const m = term.match(/\^\^<?([\w:/#.-]+)>?$/);
  if (m) return m[1];
  if (/^".*"@[\w-]+$/.test(term)) return "rdf:langString";
  if (/^".*"$/.test(term)) return "xsd:string";
  if (/^-?\d+$/.test(term)) return "xsd:integer";
  if (/^-?\d*\.\d+$/.test(term)) return "xsd:decimal";
  if (/^(true|false)$/.test(term)) return "xsd:boolean";
  return undefined;
}

/**
 * Adopt proposed shapes: each shape is validated against the CURRENT closure
 * in isolation — a shape the existing data already violates is rejected with
 * its violations (1:1 mapping, no guesswork), the clean ones are registered.
 * Versioned adoption: migrateTBox({ shapes }) instead.
 */
export async function adoptShapes(
  store: InMemoryGraphStore,
  shapes: NodeShape[],
): Promise<{ adopted: NodeShape[]; rejected: { shape: NodeShape; violations: string[] }[] }> {
  const closure = await store.match(null, null, null);
  const adopted: NodeShape[] = [];
  const rejected: { shape: NodeShape; violations: string[] }[] = [];
  for (const shape of shapes) {
    // Validate with ONLY this shape: violations map to it unambiguously.
    const violations = new InMemoryGraphStore({ shapes: [shape] }).checkShapes(closure).violations;
    if (violations.length) rejected.push({ shape, violations: violations.map((v) => v.message) });
    else adopted.push(shape);
  }
  if (adopted.length) store.addShapes(...adopted);
  return { adopted, rejected };
}

/** review_ontology agent tool: inventory + gaps, straight from the store. */
export function reviewOntologyTool(store: InMemoryGraphStore) {
  return defineTool({
    name: "review_ontology",
    description:
      "Audit the knowledge graph's ontology: classes/properties with usage counts, used-but-undeclared terms, unlabeled/unused terms, properties missing domain or range. Read-only.",
    schema: z.object({}),
    execute: async () => reviewOntology(store),
  });
}

/** propose_shapes agent tool: SHACL candidates induced from instance data. */
export function proposeShapesTool(store: InMemoryGraphStore, opts: ShapeInductionOptions = {}) {
  return defineTool({
    name: "propose_shapes",
    description:
      "Induce candidate SHACL shapes from existing instance data (>= 2 instances per class, >= 50% property coverage). PROPOSAL: adoption is the builder's call via adoptShapes(), never automatic.",
    schema: z.object({}),
    execute: async () => proposeShapes(store, opts),
  });
}