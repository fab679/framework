/**
 * Semantic layer. See docs/07-semantics-governance.md.
 * In-memory store: RDFS-subset + OWL property axioms entailment with proofs,
 * SHACL-subset validation, versioned CONSTRUCT-style rules, SPARQL-subset
 * read tools, and GraphMemory. Remote SPARQL, entity linking + JSON-LD I/O,
 * and TBox evolution live in sibling modules, re-exported here.
 */

export interface Quad {
  subject: string;
  predicate: string;
  object: string;
  graph?: string;
  /** Provenance: who vouched, source URI, valid-until. Required on agent writes. */
  source?: string;
  derivedFrom?: string;
  rule?: string;
}

export interface ShapeReport {
  valid: boolean;
  violations: { message: string; node?: string; path?: string }[];
}

/** Minimal triple-store contract — N3 in-memory first, remote SPARQL later. */
export interface GraphStore {
  add(quads: Quad[]): Promise<void>;
  /**
   * Match by pattern; null/undefined = wildcard (including the graph slot).
   * Queries the entailment closure. A `graph` value selects ONE named
   * graph — the same s/p/o CAN live in several graphs; identity includes it.
   */
  match(s?: string | null, p?: string | null, o?: string | null, g?: string | null): Promise<Quad[]>;
  validate(quads: Quad[]): Promise<ShapeReport>;
  /** Proof chain for an entailed fact: premises + rule id. Null for explicit facts. */
  why(quad: Quad): Promise<{ premises: Quad[]; rule: string } | null>;
}

const RDF_TYPE = new Set(["rdf:type", "a", "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"]);
const SUB_CLASS = new Set(["rdfs:subClassOf", "http://www.w3.org/2000/01/rdf-schema#subClassOf"]);
const SUB_PROP = new Set(["rdfs:subPropertyOf", "http://www.w3.org/2000/01/rdf-schema#subPropertyOf"]);
const DOMAIN = new Set(["rdfs:domain", "http://www.w3.org/2000/01/rdf-schema#domain"]);
const RANGE = new Set(["rdfs:range", "http://www.w3.org/2000/01/rdf-schema#range"]);
const OWL_TRANSITIVE = new Set(["owl:TransitiveProperty", "http://www.w3.org/2002/07/owl#TransitiveProperty"]);
const OWL_SYMMETRIC = new Set(["owl:SymmetricProperty", "http://www.w3.org/2002/07/owl#SymmetricProperty"]);
const OWL_FUNCTIONAL = new Set(["owl:FunctionalProperty", "http://www.w3.org/2002/07/owl#FunctionalProperty"]);
const OWL_INVERSE = new Set(["owl:inverseOf", "http://www.w3.org/2002/07/owl#inverseOf"]);

/** SHACL-subset shape: class-scoped property constraints. */
export interface PropertyShape {
  path: string;
  minCount?: number;
  maxCount?: number;
  datatype?: string;
  nodeKind?: "iri" | "literal";
  hasValue?: string;
}

export interface NodeShape {
  targetClass: string;
  properties: PropertyShape[];
}

/** Versioned CONSTRUCT-style rule over triple patterns (?vars). */
export interface TriplePattern {
  subject: string;
  predicate: string;
  object: string;
}

export interface GraphRule {
  id: string;
  version?: string;
  where: TriplePattern[];
  construct: TriplePattern[];
}

export interface StoreOptions {
  shapes?: NodeShape[];
  rules?: GraphRule[];
}

function isLiteral(term: string): boolean {
  if (term.startsWith('"')) return true;
  if (/^(-?\d+(\.\d+)?|true|false)$/.test(term)) return true;
  if (term.startsWith("<") || /^[a-zA-Z][\w-]*:/.test(term)) return false;
  return true;
}

function termDatatype(term: string): string | undefined {
  const m = term.match(/\^\^<?([\w:/#.-]+)>?$/);
  if (m) return m[1];
  if (/^".*"@[\w-]+$/.test(term)) return "rdf:langString";
  if (/^".*"$/.test(term)) return "xsd:string";
  if (/^-?\d+$/.test(term)) return "xsd:integer";
  if (/^-?\d*\.\d+$/.test(term)) return "xsd:decimal";
  if (/^(true|false)$/.test(term)) return "xsd:boolean";
  return undefined;
}

export class InMemoryGraphStore implements GraphStore {
  private quads: Quad[] = [];
  private derivations = new Map<string, { premises: Quad[]; rule: string }>();
  private shapes: NodeShape[] = [];
  private rules: GraphRule[] = [];

  constructor(opts: StoreOptions = {}) {
    this.shapes = [...(opts.shapes ?? [])];
    this.rules = [...(opts.rules ?? [])];
  }

  addShapes(...shapes: NodeShape[]): this {
    this.shapes.push(...shapes);
    return this;
  }

  addRules(...rules: GraphRule[]): this {
    this.rules.push(...rules);
    return this;
  }

  /** Registered SHACL shapes (carried to new stores by TBox migrations). */
  ontologyShapes(): NodeShape[] {
    return [...this.shapes];
  }

  /** Registered CONSTRUCT rules (versioned with the ontology they encode). */
  ontologyRules(): GraphRule[] {
    return [...this.rules];
  }

  /** Identity includes the named graph: the same s/p/o in two graphs are two quads. */
  private key(q: Pick<Quad, "subject" | "predicate" | "object" | "graph">): string {
    return `${q.subject}|${q.predicate}|${q.object}|${q.graph ?? ""}`;
  }

  async add(quads: Quad[]): Promise<void> {
    const report = await this.validate(quads);
    if (!report.valid) {
      throw new Error(`SHACL violation: ${report.violations.map((v) => v.message).join("; ")}`);
    }
    const known = new Set(this.quads.map((q) => this.key(q)));
    const addedKeys: string[] = [];
    for (const q of quads) {
      if (!known.has(this.key(q))) {
        known.add(this.key(q));
        addedKeys.push(this.key(q));
        this.quads.push(q);
      }
    }
    const rollback = (): void => {
      const doomed = new Set(addedKeys);
      this.quads = this.quads.filter((q) => !doomed.has(this.key(q)));
      for (const k of doomed) this.derivations.delete(k);
    };
    const conflicts = this.checkFunctional();
    if (conflicts.length) {
      rollback();
      throw new Error(`Functional property violation: ${conflicts.join("; ")}`);
    }
    const shapeReport = this.checkShapes(this.quads);
    if (!shapeReport.valid) {
      rollback();
      throw new Error(`SHACL violation: ${shapeReport.violations.map((v) => v.message).join("; ")}`);
    }
    this.entailToFixpoint(known);
  }

  async match(s?: string | null, p?: string | null, o?: string | null, g?: string | null): Promise<Quad[]> {
    return this.quads.filter(
      (q) =>
        (!s || q.subject === s) && (!p || q.predicate === p) && (!o || q.object === o) && (!g || q.graph === g),
    );
  }

  /** Structural + provenance check. Full SHACL engine lands in P1-sem. */
  async validate(quads: Quad[]): Promise<ShapeReport> {
    const violations = quads
      .filter((q) => !q.subject || !q.predicate || !q.object)
      .map((q) => ({ message: `Incomplete triple: ${JSON.stringify(q)}` }));
    const noSource = quads
      .filter((q) => !q.source)
      .map((q) => ({ message: `Missing provenance (source) on ${q.subject} ${q.predicate} ${q.object}` }));
    return { valid: violations.length + noSource.length === 0, violations: [...violations, ...noSource] };
  }

  async why(quad: Quad): Promise<{ premises: Quad[]; rule: string } | null> {
    return this.derivations.get(this.key(quad)) ?? null;
  }

  /** Functional properties must have at most one value per subject (conflict, not a second fact). */
  private checkFunctional(): string[] {
    const functionalProps = new Set(
      this.quads.filter((q) => RDF_TYPE.has(q.predicate) && OWL_FUNCTIONAL.has(q.object)).map((q) => q.subject),
    );
    const conflicts: string[] = [];
    for (const prop of functionalProps) {
      const bySubject = new Map<string, Set<string>>();
      for (const q of this.quads) {
        if (q.predicate !== prop) continue;
        const values = bySubject.get(q.subject) ?? new Set<string>();
        values.add(q.object);
        bySubject.set(q.subject, values);
      }
      for (const [subject, values] of bySubject) {
        if (values.size > 1) conflicts.push(`${subject} has ${values.size} values for functional ${prop}`);
      }
    }
    return conflicts;
  }

  /** SHACL-subset validation over a quad set. */
  checkShapes(quads: Quad[]): ShapeReport {
    const violations: ShapeReport["violations"] = [];
    for (const shape of this.shapes) {
      const targets = quads.filter((q) => RDF_TYPE.has(q.predicate) && q.object === shape.targetClass).map((q) => q.subject);
      for (const node of new Set(targets)) {
        for (const prop of shape.properties) {
          const values = quads.filter((q) => q.subject === node && q.predicate === prop.path).map((q) => q.object);
          if (prop.minCount != null && values.length < prop.minCount) {
            violations.push({ message: `${node} needs >= ${prop.minCount} ${prop.path}`, node, path: prop.path });
          }
          if (prop.maxCount != null && values.length > prop.maxCount) {
            violations.push({ message: `${node} allows <= ${prop.maxCount} ${prop.path}`, node, path: prop.path });
          }
          for (const v of values) {
            if (prop.datatype && termDatatype(v) !== prop.datatype && !(prop.datatype === "xsd:string" && isLiteral(v))) {
              violations.push({ message: `${node} ${prop.path}=${v} is not ${prop.datatype}`, node, path: prop.path });
            }
            if (prop.nodeKind === "iri" && isLiteral(v)) {
              violations.push({ message: `${node} ${prop.path}=${v} must be an IRI`, node, path: prop.path });
            }
            if (prop.nodeKind === "literal" && !isLiteral(v)) {
              violations.push({ message: `${node} ${prop.path}=${v} must be a literal`, node, path: prop.path });
            }
            if (prop.hasValue !== undefined && v !== prop.hasValue) {
              violations.push({ message: `${node} ${prop.path} must be ${prop.hasValue}`, node, path: prop.path });
            }
          }
        }
      }
    }
    return { valid: violations.length === 0, violations };
  }

  private insert(q: Quad, premises: Quad[], rule: string, known: Set<string>): boolean {
    const k = this.key(q);
    if (known.has(k)) return false;
    known.add(k);
    this.quads.push(q);
    this.derivations.set(k, { premises, rule });
    return true;
  }

  /**
   * RDFS subset + OWL property axioms + versioned rules, to fixpoint:
   * - RDFS-SUB-TRANS / RDFS-TYPE / RDFS-SUBPROP-TRANS / RDFS-SUBPROP / RDFS-DOMAIN / RDFS-RANGE
   * - OWL-TRANSITIVE / OWL-SYMMETRIC / OWL-INVERSE (inverseOf both directions)
   * - <ruleId>@<version> for each applied GraphRule (premises tracked for why())
   */
  private entailToFixpoint(known: Set<string>): void {
    for (;;) {
      let added = false;
      const snapshot = [...this.quads];
      const byPred = (ps: Set<string>) => snapshot.filter((q) => ps.has(q.predicate));
      const insert = (q: Quad, premises: Quad[], rule: string): boolean => {
        const addedOne = this.insert(q, premises, rule, known);
        added = addedOne || added;
        return addedOne;
      };

      for (const ab of byPred(SUB_CLASS)) {
        for (const bc of byPred(SUB_CLASS).filter((q) => q.subject === ab.object)) {
          added = this.insert(
            { subject: ab.subject, predicate: ab.predicate, object: bc.object, source: "entailment", rule: "RDFS-SUB-TRANS" },
            [ab, bc],
            "RDFS-SUB-TRANS",
            known,
          ) || added;
        }
      }
      for (const t of byPred(RDF_TYPE)) {
        for (const sc of byPred(SUB_CLASS).filter((q) => q.subject === t.object)) {
          added = this.insert(
            { subject: t.subject, predicate: t.predicate, object: sc.object, source: "entailment", rule: "RDFS-TYPE" },
            [t, sc],
            "RDFS-TYPE",
            known,
          ) || added;
        }
      }
      for (const ab of byPred(SUB_PROP)) {
        for (const bc of byPred(SUB_PROP).filter((q) => q.subject === ab.object)) {
          added = this.insert(
            { subject: ab.subject, predicate: ab.predicate, object: bc.object, source: "entailment", rule: "RDFS-SUBPROP-TRANS" },
            [ab, bc],
            "RDFS-SUBPROP-TRANS",
            known,
          ) || added;
        }
        for (const use of snapshot.filter((q) => q.predicate === ab.subject)) {
          added = this.insert(
            { subject: use.subject, predicate: ab.object, object: use.object, source: "entailment", rule: "RDFS-SUBPROP" },
            [ab, use],
            "RDFS-SUBPROP",
            known,
          ) || added;
        }
      }
      for (const d of byPred(DOMAIN)) {
        for (const use of snapshot.filter((q) => q.predicate === d.subject)) {
          added = this.insert(
            { subject: use.subject, predicate: "rdf:type", object: d.object, source: "entailment", rule: "RDFS-DOMAIN" },
            [d, use],
            "RDFS-DOMAIN",
            known,
          ) || added;
        }
      }
      for (const r of byPred(RANGE)) {
        for (const use of snapshot.filter((q) => q.predicate === r.subject)) {
          if (insert(
            { subject: use.object, predicate: "rdf:type", object: r.object, source: "entailment", rule: "RDFS-RANGE" },
            [r, use],
            "RDFS-RANGE",
          )) added = true;
        }
      }
      added = this.applyOwl(snapshot, insert) || added;
      added = this.applyRules(snapshot, insert) || added;
      if (!added) return;
    }
  }

  private applyOwl(snapshot: Quad[], insert: (q: Quad, premises: Quad[], rule: string) => boolean): boolean {
    let added = false;
    const transitive = new Set(
      snapshot.filter((q) => RDF_TYPE.has(q.predicate) && OWL_TRANSITIVE.has(q.object)).map((q) => q.subject),
    );
    const symmetric = new Set(
      snapshot.filter((q) => RDF_TYPE.has(q.predicate) && OWL_SYMMETRIC.has(q.object)).map((q) => q.subject),
    );
    for (const p of transitive) {
      const uses = snapshot.filter((q) => q.predicate === p);
      for (const xy of uses) {
        for (const yz of uses.filter((q) => q.subject === xy.object)) {
          if (insert({ subject: xy.subject, predicate: p, object: yz.object, source: "entailment", rule: "OWL-TRANSITIVE" }, [xy, yz], "OWL-TRANSITIVE")) added = true;
        }
      }
    }
    for (const p of symmetric) {
      for (const xy of snapshot.filter((q) => q.predicate === p)) {
        if (insert({ subject: xy.object, predicate: p, object: xy.subject, source: "entailment", rule: "OWL-SYMMETRIC" }, [xy], "OWL-SYMMETRIC")) added = true;
      }
    }
    for (const inv of snapshot.filter((q) => OWL_INVERSE.has(q.predicate))) {
      for (const use of snapshot.filter((q) => q.predicate === inv.subject || q.predicate === inv.object)) {
        const flipped = use.predicate === inv.subject ? inv.object : inv.subject;
        if (insert({ subject: use.object, predicate: flipped, object: use.subject, source: "entailment", rule: "OWL-INVERSE" }, [inv, use], "OWL-INVERSE")) added = true;
      }
    }
    return added;
  }

  private applyRules(snapshot: Quad[], insert: (q: Quad, premises: Quad[], rule: string) => boolean): boolean {
    let added = false;
    for (const rule of this.rules) {
      const tag = rule.version ? `${rule.id}@${rule.version}` : rule.id;
      for (const binding of matchPatterns(rule.where, snapshot)) {
        for (const pattern of rule.construct) {
          const built: TriplePattern = {
            subject: instantiate(pattern.subject, binding.vars),
            predicate: instantiate(pattern.predicate, binding.vars),
            object: instantiate(pattern.object, binding.vars),
          };
          if (built.subject == null || built.predicate == null || built.object == null) continue;
          if (insert(
            { subject: built.subject, predicate: built.predicate, object: built.object, source: "entailment", rule: tag, derivedFrom: tag },
            binding.quads,
            tag,
          )) added = true;
        }
      }
    }
    return added;
  }
}

// ---------------------------------------------------------------------------
// Pattern matching + SPARQL-subset (SELECT with conjunctive triple patterns)
// ---------------------------------------------------------------------------

export interface PatternBinding {
  vars: Map<string, string>;
  quads: Quad[];
}

function isVar(term: string): boolean {
  return term.startsWith("?");
}

function matchTerm(pattern: string, value: string, vars: Map<string, string>): Map<string, string> | undefined {
  if (isVar(pattern)) {
    const bound = vars.get(pattern);
    if (bound === undefined) {
      const next = new Map(vars);
      next.set(pattern, value);
      return next;
    }
    return bound === value ? vars : undefined;
  }
  return pattern === value ? vars : undefined;
}

function matchOne(pattern: TriplePattern, quad: Quad, vars: Map<string, string>): Map<string, string> | undefined {
  let v = matchTerm(pattern.subject, quad.subject, vars);
  if (!v) return undefined;
  v = matchTerm(pattern.predicate, quad.predicate, v);
  if (!v) return undefined;
  return matchTerm(pattern.object, quad.object, v);
}

/** Conjunctive matching of patterns against quads (nested-loop join). */
export function matchPatterns(patterns: TriplePattern[], quads: Quad[]): PatternBinding[] {
  let bindings: PatternBinding[] = [{ vars: new Map(), quads: [] }];
  for (const pattern of patterns) {
    const next: PatternBinding[] = [];
    for (const b of bindings) {
      for (const q of quads) {
        const vars = matchOne(pattern, q, b.vars);
        if (vars) next.push({ vars, quads: [...b.quads, q] });
      }
    }
    bindings = next;
    if (!bindings.length) break;
  }
  return bindings;
}

export function instantiate(term: string, vars: Map<string, string>): string {
  return isVar(term) ? (vars.get(term) ?? term) : term;
}

export interface SparqlSelect {
  variables: string[];
  patterns: TriplePattern[];
}

function tokenizeSparql(input: string): string[] {
  const tokens: string[] = [];
  const re = /"[^"]*"(?:\^\^<?[\w:/#.-]+>?|@[\w-]+)?|<[^>]*>|[{}().;]|\S+/g;
  let m: RegExpExecArray | null;
  while ((m = re.exec(input)) !== null) tokens.push(m[0]);
  return tokens;
}

/**
 * Minimal SELECT parser: `SELECT ?a ?b WHERE { s p o . ... }`.
 * Terms: ?vars, <iris>, prefixed names, "literals" (^^datatype/@lang ok).
 * Anything else throws — this is a documented subset, not full SPARQL.
 */
export function parseSelect(query: string): SparqlSelect {
  const tokens = tokenizeSparql(query);
  let i = 0;
  const expect = (want: string): void => {
    if (tokens[i]?.toUpperCase() !== want) throw new Error(`SPARQL: expected ${want}, got ${tokens[i] ?? "end"}`);
    i += 1;
  };
  expect("SELECT");
  const variables: string[] = [];
  while (i < tokens.length && tokens[i].startsWith("?")) {
    variables.push(tokens[i]);
    i += 1;
  }
  if (!variables.length) throw new Error("SPARQL: SELECT needs at least one variable");
  expect("WHERE");
  expect("{");
  const patterns: TriplePattern[] = [];
  while (i < tokens.length && tokens[i] !== "}") {
    const s = tokens[i++];
    const p = tokens[i++];
    const o = tokens[i++];
    if (s == null || p == null || o == null || s === "." || p === "." || o === ".") {
      throw new Error("SPARQL: malformed triple pattern");
    }
    patterns.push({ subject: s, predicate: p, object: o });
    if (tokens[i] === ".") i += 1;
  }
  expect("}");
  return { variables, patterns };
}

/** Execute a SELECT against the store closure. Returns variable bindings. */
export async function executeSelect(store: GraphStore, query: string): Promise<Record<string, string>[]> {
  const { variables, patterns } = parseSelect(query);
  const quads = await store.match(null, null, null);
  return matchPatterns(patterns, quads).map((b) => {
    const row: Record<string, string> = {};
    for (const v of variables) row[v] = b.vars.get(v) ?? "";
    return row;
  });
}

// ---------------------------------------------------------------------------
// Agent tools: read, write (autonomous, validated), verify
// ---------------------------------------------------------------------------

import { z } from "zod";
import { defineTool } from "../tools/index.js";

/** Read-only SPARQL SELECT over the entailment closure. */
export function sparqlSelectTool(store: GraphStore) {
  return defineTool({
    name: "sparql_select",
    description: "Query the knowledge graph. Subset: SELECT ?vars WHERE { s p o . ... } over the inferred closure.",
    schema: z.object({ query: z.string().min(1).max(4000) }),
    execute: async ({ query }: { query: string }) => executeSelect(store, query),
  });
}

/** Ontology lookup: find classes/properties/labels matching a term. */
export function ontologyLookupTool(store: GraphStore) {
  return defineTool({
    name: "ontology_lookup",
    description: "Look up ontology terms (classes, properties, labels) instead of inventing predicates.",
    schema: z.object({ term: z.string().min(1).max(200), limit: z.number().int().min(1).max(20).default(8) }),
    execute: async ({ term, limit }: { term: string; limit?: number }) => {
      const needle = term.toLowerCase();
      const quads = await store.match(null, null, null);
      return quads
        .filter((q) => `${q.subject} ${q.predicate} ${q.object}`.toLowerCase().includes(needle))
        .slice(0, limit ?? 8)
        .map((q) => ({ subject: q.subject, predicate: q.predicate, object: q.object }));
    },
  });
}

export const TurtleQuadSchema = z.object({
  subject: z.string().min(1),
  predicate: z.string().min(1),
  object: z.string().min(1),
  source: z.string().min(1).describe("Provenance URI (required — writes without it are rejected)"),
});

/**
 * Autonomous validated write: SHACL + provenance + functional checks run
 * inside the store. Violations return as tool results (not throws) so the
 * model repairs and retries on its own — autonomy without anarchy.
 */
export function graphAddTool(store: GraphStore) {
  return defineTool({
    name: "graph_add",
    description: "Write facts to the knowledge graph. Validated (SHACL/provenance/functional); violations return repair feedback.",
    schema: z.object({ quads: z.array(TurtleQuadSchema).min(1).max(50) }),
    execute: async ({ quads }: { quads: { subject: string; predicate: string; object: string; source: string }[] }) => {
      try {
        await store.add(quads);
        const inferred = (await store.match(null, null, null)).filter((q) => q.source === "entailment");
        return { committed: quads.length, inferred: inferred.length };
      } catch (err) {
        return `Validation failed — fix and retry: ${err instanceof Error ? err.message : String(err)}`;
      }
    },
    toResult: (output: unknown) => ({
      content: typeof output === "string" ? output : JSON.stringify(output),
      artifact: typeof output === "string" ? undefined : { committed: (output as { committed: number }).committed },
    }),
  });
}

export type ClaimVerdict = "grounded" | "unknown" | "contradicted";

/**
 * Anti-hallucination primitive: is this triple in the closure (grounded),
 * absent (unknown — do not assert), or clashing with a functional value
 * (contradicted)? Ask before answering from memory.
 */
export function claimCheckTool(store: GraphStore) {
  return defineTool({
    name: "claim_check",
    description: "Verify a fact against the graph before asserting it. Returns grounded | unknown | contradicted with proof.",
    schema: z.object({ subject: z.string().min(1), predicate: z.string().min(1), object: z.string().min(1) }),
    execute: async ({ subject, predicate, object }: { subject: string; predicate: string; object: string }) => {
      const direct = await store.match(subject, predicate, object);
      if (direct.length) {
        return { verdict: "grounded" as ClaimVerdict, proof: await store.why(direct[0]) };
      }
      const siblings = await store.match(subject, predicate, null);
      const declarations = await store.match(predicate, null, null);
      const functional = declarations.some((q) => RDF_TYPE.has(q.predicate) && OWL_FUNCTIONAL.has(q.object));
      const clash = siblings.filter((q) => q.object !== object);
      if (functional && clash.length) {
        return {
          verdict: "contradicted" as ClaimVerdict,
          proof: { existing: clash.map((q) => q.object) },
        };
      }
      return {
        verdict: "unknown" as ClaimVerdict,
        proof: siblings.length ? { related: siblings.map((q) => q.object) } : null,
      };
    },
  });
}

// ---------------------------------------------------------------------------
// GraphMemory: closure-aware session memory + long-term store
// ---------------------------------------------------------------------------

import type { ChatMessage } from "../types/index.js";
import type { Memory } from "../memory/index.js";
import { tokenize } from "../skills/index.js";

/**
 * Session memory + long-term store in one:
 * - load(): recent messages + top-k closure facts relevant to the latest
 *   user turn (keyword overlap), rendered as a knowledge message.
 * - save(): delegates to inner memory (extraction of triples from prose
 *   stays model-driven via graph_add — never silent parsing).
 * - exportState()/importState(): quads JSON for SessionManager snapshots (LTS).
 */
export class GraphMemory implements Memory {
  constructor(
    private store: GraphStore,
    private inner: Memory,
    private opts: { facts?: number } = {},
  ) {}

  async load(): Promise<ChatMessage[]> {
    const messages = await this.inner.load();
    const lastUser = [...messages].reverse().find((m) => m.role === "user");
    if (!lastUser) return messages;
    const terms = new Set(tokenize(typeof lastUser.content === "string" ? lastUser.content : ""));
    if (!terms.size) return messages;
    const quads = await this.store.match(null, null, null);
    const scored = quads
      .map((q) => {
        const hay = `${q.subject} ${q.predicate} ${q.object}`.toLowerCase();
        let score = 0;
        for (const t of terms) if (hay.includes(t)) score += 1;
        return { q, score };
      })
      .filter((s) => s.score > 0)
      .sort((a, b) => b.score - a.score)
      .slice(0, this.opts.facts ?? 8);
    if (!scored.length) return messages;
    const lines = scored.map((s) => `- ${s.q.subject} ${s.q.predicate} ${s.q.object}`);
    return [...messages, { role: "assistant", content: `Relevant known facts (ground answers in these):\n${lines.join("\n")}` }];
  }

  async save(messages: ChatMessage[]): Promise<void> {
    await this.inner.save(messages);
  }

  async clear(): Promise<void> {
    await this.inner.clear();
  }

  /** Serialize the closure for snapshots / long-term persistence. */
  async exportState(): Promise<{ quads: Quad[] }> {
    return { quads: await this.store.match(null, null, null) };
  }

  /** Restore a previously exported closure (facts become explicit again). */
  async importState(state: { quads: Quad[] }): Promise<void> {
    const fresh = state.quads.filter((q) => q.source !== "entailment");
    if (fresh.length) await this.store.add(fresh);
  }
}

export * from "./remote-sparql.js";
export * from "./entity-linking.js";
export * from "./ontology-evolution.js";
export * from "./ontology-review.js";
