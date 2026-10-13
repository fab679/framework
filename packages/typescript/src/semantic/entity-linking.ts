import { z } from "zod";
import { defineTool } from "../tools/index.js";
import type { GraphStore, Quad } from "./index.js";

/**
 * Entity linking (roadmap P2-sem): turn free-text mentions into graph IRIs.
 * Case-insensitive label index (rdfs:label / skos:prefLabel / skos:altLabel)
 * over the store closure, canonicalized through owl:sameAs groups — a
 * mention linked through any alias resolves to ONE representative IRI.
 *
 * docs/07 rule 2, autonomy-first: the model calls `propose_mapping` and the
 * mapping commits on proposal (default) as a provenance-carrying
 * skos:altLabel the linker sees from then on — matching the autonomous write
 * path everywhere else (graph_add). Human review is the opt-out:
 * `new MappingProposals({ autonomy: false })` queues proposals for decide(),
 * or gate the tool with humanInTheLoopMiddleware. Provenance is mandatory
 * either way.
 */

export const LABEL_PREDICATES = new Set([
  "rdfs:label",
  "http://www.w3.org/2000/01/rdf-schema#label",
  "skos:prefLabel",
  "http://www.w3.org/2004/02/skos/core#prefLabel",
  "skos:altLabel",
  "http://www.w3.org/2004/02/skos/core#altLabel",
]);
const SAME_AS = new Set(["owl:sameAs", "http://www.w3.org/2002/07/owl#sameAs"]);

/** Prefixes used to expand/compact IRIs for JSON-LD I/O (nullable values pass through). */
export const JSONLD_PREFIXES: Record<string, string> = {
  rdf: "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
  rdfs: "http://www.w3.org/2000/01/rdf-schema#",
  owl: "http://www.w3.org/2002/07/owl#",
  xsd: "http://www.w3.org/2001/XMLSchema#",
  skos: "http://www.w3.org/2004/02/skos/core#",
  dct: "http://purl.org/dc/terms/",
  prov: "http://www.w3.org/ns/prov#",
  schema: "https://schema.org/",
  ex: "urn:example:",
};

export interface LinkedEntity {
  /** Canonical (sameAs-group representative) IRI, in the store's term form. */
  iri: string;
  label?: string;
  score: number;
  /** Other IRIs the same entity is known as (the sameAs group). */
  sameAsGroup: string[];
}

export class EntityLinker {
  constructor(private store: GraphStore) {}

  /** Union-find over owl:sameAs pairs: every alias cluster -> one representative. */
  private async sameAsGroups(): Promise<Map<string, string>> {
    const pairs = (await this.store.match(null, null, null)).filter((q) => SAME_AS.has(q.predicate));
    const parent = new Map<string, string>();
    const find = (x: string): string => {
      let root = x;
      while (parent.get(root) !== root && parent.has(root)) root = parent.get(root)!;
      return root;
    };
    const union = (a: string, b: string): void => {
      const ra = find(a);
      const rb = find(b);
      if (ra !== rb) parent.set(ra, rb);
    };
    for (const q of pairs) {
      if (!parent.has(q.subject)) parent.set(q.subject, q.subject);
      if (!parent.has(q.object)) parent.set(q.object, q.object);
      union(q.subject, q.object);
    }
    const representative = new Map<string, string>();
    for (const member of parent.keys()) representative.set(member, find(member));
    return representative;
  }

  /** Mention -> candidate entities, best first, canonicalized via sameAs. */
  async link(mention: string, k = 5): Promise<LinkedEntity[]> {
    const needle = mention.trim().toLowerCase();
    if (!needle) return [];
    const quads = await this.store.match(null, null, null);
    const canonical = await this.sameAsGroups();
    const labels = new Map<string, string>(); // iri -> best label (pref > rdfs > alt)
    const labelScore = new Map<string, number>(); // iri -> score of best label predicate (lower wins)

    const candidates: { iri: string; label?: string; score: number }[] = [];
    for (const q of quads) {
      // IRI local-name match (ex:ada -> "ada", also the full-IRI tail).
      const localPart = localName(q.subject);
      const fullTail = q.subject.includes("/") || q.subject.includes("#") ? q.subject.split(/[#/]/).pop() ?? "" : localPart;
      if (localPart === needle) candidates.push({ iri: q.subject, score: 40 });
      else if (localPart.startsWith(needle)) candidates.push({ iri: q.subject, score: 25 });
      else if (fullTail.toLowerCase() === needle) candidates.push({ iri: q.subject, score: 40 });

      if (LABEL_PREDICATES.has(q.predicate)) {
        const label = stripLiteral(q.object).toLowerCase();
        const trust = q.predicate.includes("altLabel") ? 2 : q.predicate.includes("prefLabel") ? 0 : 1;
        if (label === needle) candidates.push({ iri: q.subject, label: stripLiteral(q.object), score: 100 });
        else if (label.startsWith(needle)) candidates.push({ iri: q.subject, label: stripLiteral(q.object), score: 60 });
        else if (label.includes(needle)) candidates.push({ iri: q.subject, label: stripLiteral(q.object), score: 30 });
        // Track each iri's best (most authoritative) label for output.
        const prev = labelScore.get(q.subject);
        if (prev === undefined || trust < prev) {
          labels.set(q.subject, stripLiteral(q.object));
          labelScore.set(q.subject, trust);
        }
      }
    }

    // Merge per canonical IRI, keep the best score + the group members.
    const merged = new Map<string, LinkedEntity>();
    for (const c of candidates) {
      const rep = canonical.get(c.iri) ?? c.iri;
      const existing = merged.get(rep);
      if (existing) {
        existing.score = Math.max(existing.score, c.score);
        if (c.label && !existing.label) existing.label = c.label;
        if (!existing.sameAsGroup.includes(c.iri)) existing.sameAsGroup.push(c.iri);
      } else {
        merged.set(rep, {
          iri: rep,
          label: c.label ?? labels.get(rep),
          score: c.score,
          sameAsGroup: [c.iri, ...(rep !== c.iri ? [rep] : [])],
        });
      }
    }
    for (const group of merged.values()) {
      group.label ??= labels.get(group.iri);
      if (group.sameAsGroup.length < 2) {
        const members = [...canonical.keys()].filter((m) => canonical.get(m) === canonical.get(group.iri));
        if (members.length > 1) group.sameAsGroup = members;
      }
    }
    return [...merged.values()]
      .sort((a, b) => b.score - a.score)
      .slice(0, k);
  }
}

function stripLiteral(term: string): string {
  const m = term.match(/^"(.*)"(\^\^<?[\w:/#.-]+>?|@[\w-]+)?$/);
  return m ? m[1] : term;
}

function localName(iri: string): string {
  const tail = iri.split(/[#/:]/).pop() ?? "";
  return tail.toLowerCase();
}

/** link_entity agent tool: mention -> canonical IRIs with scores. */
export function linkEntityTool(linker: EntityLinker) {
  return defineTool({
    name: "link_entity",
    description:
      "Link a text mention to known graph entities (labels + sameAs canonicalized). Returns canonical IRIs with scores — never invent IRIs.",
    schema: z.object({ mention: z.string().min(1).max(200), k: z.number().int().min(1).max(10).default(5) }),
    execute: async ({ mention, k }: { mention: string; k?: number }) => linker.link(mention, k),
  });
}

// ---------------------------------------------------------------------------
// Mapping proposals: LLM proposes, humans approve (docs/07 rule 2)
// ---------------------------------------------------------------------------

export interface MappingProposal {
  id: string;
  /** The source-side term: usually a raw column header / mention string. */
  from: string;
  /** The proposed target: a canonical ontology IRI. */
  to: string;
  /** Provenance for the eventual commit (required by GraphStore.validate). */
  source: string;
  status: "pending" | "approved" | "rejected";
  /** True when autonomy committed it on proposal (no human turn). */
  committedByAutonomy?: boolean;
  proposedAt: string;
}

export type MappingDecision = "approved" | "rejected";

export interface MappingProposalsOptions {
  /**
   * Autonomy: proposals commit IMMEDIATELY on proposal. DEFAULT ON — the
   * agent's whole write path is autonomous (graph_add), so mapping should
   * not be the one gate keeping a human in the loop. Set `autonomy: false`
   * to queue proposals for human decide() instead, or gate the tool with
   * humanInTheLoopMiddleware.
   */
  autonomy?: boolean;
  /** Store to commit against. Without one there is nowhere to commit — proposals queue as pending. */
  store?: GraphStore;
}

export class MappingProposals {
  private seq = 0;
  private items = new Map<string, MappingProposal>();

  constructor(private opts: MappingProposalsOptions = {}) {}

  /** True when proposals commit themselves (autonomy on AND a store bound). */
  get autonomy(): boolean {
    return this.opts.autonomy !== false && !!this.opts.store;
  }

  private async commit(proposal: MappingProposal, store: GraphStore): Promise<void> {
    // Promotion = a provenance-carrying alias the linker can see forever.
    await store.add([
      {
        subject: proposal.to,
        predicate: "skos:altLabel",
        object: `"${proposal.from}"`,
        source: proposal.source,
        derivedFrom: `mapping:${proposal.id}`,
      },
    ]);
  }

  /**
   * Record an LLM-proposed mapping. With autonomy (default) + a bound store
   * it commits right away, provenance required; with `autonomy: false` (or
   * no store bound) it queues as pending until decide().
   */
  async propose(from: string, to: string, source: string): Promise<MappingProposal> {
    const existing = [...this.items.values()].find(
      (p) => p.from === from && p.to === to && (p.status === "pending" || (this.autonomy && p.status === "approved")),
    );
    if (existing) return existing;
    const proposal: MappingProposal = {
      id: `map-${++this.seq}`,
      from,
      to,
      source,
      status: "pending",
      proposedAt: new Date().toISOString(),
    };
    if (this.autonomy && this.opts.store) {
      await this.commit(proposal, this.opts.store);
      proposal.status = "approved";
      proposal.committedByAutonomy = true;
    }
    this.items.set(proposal.id, proposal);
    return proposal;
  }

  list(status?: MappingProposal["status"]): MappingProposal[] {
    return [...this.items.values()].filter((p) => !status || p.status === status);
  }

  /** Human decision. `approved` also commits the alias into the graph. */
  async decide(store: GraphStore, id: string, decision: MappingDecision): Promise<MappingProposal | undefined> {
    const proposal = this.items.get(id);
    if (!proposal || proposal.status !== "pending") return undefined;
    proposal.status = decision;
    if (decision === "approved") {
      await this.commit(proposal, store);
    }
    return proposal;
  }
}

export function proposeMappingTool(proposals: MappingProposals) {
  return defineTool({
    name: "propose_mapping",
    description: "Map a raw term to a canonical ontology IRI. Autonomy mode commits the alias immediately (provenance required); review mode queues it for human approval.",
    schema: z.object({
      from: z.string().min(1).max(200),
      to: z.string().min(1).max(500),
      source: z.string().min(1).describe("Provenance URI for the committed alias"),
    }),
    execute: async ({ from, to, source }: { from: string; to: string; source: string }) => {
      let p: MappingProposal;
      try {
        p = await proposals.propose(from, to, source);
      } catch (err) {
        // Same contract as graph_add: violations are repair feedback, not a
        // thrown tool error that would kill the run.
        return `Commit rejected — fix and retry: ${err instanceof Error ? err.message : String(err)}`;
      }
      return p.committedByAutonomy
        ? { id: p.id, status: "approved", committed: true }
        : { id: p.id, status: "pending", note: "Review mode: awaits a human decide() before writing." };
    },
  });
}

// ---------------------------------------------------------------------------
// JSON-LD expand/compact I/O (roadmap P2-sem)
// ---------------------------------------------------------------------------

interface JsonLdValue {
  "@id"?: string;
  "@value"?: string;
  "@type"?: string;
  "@language"?: string;
}

function resolveIri(term: string, prefixes: Record<string, string>): string | undefined {
  const m = term.match(/^([a-zA-Z][\w-]*):(.+)$/);
  const base = m ? prefixes[m[1]] : undefined;
  if (m && base) return `${base}${m[2]}`;
  if (/^https?:\/\//.test(term)) return term;
  return undefined; // literal or unknown prefix
}

function compactIri(iri: string, prefixes: Record<string, string>): string {
  for (const [prefix, base] of Object.entries(prefixes)) {
    if (iri.startsWith(base)) return `${prefix}:${iri.slice(base.length)}`;
  }
  return iri;
}

function termToJsonLd(term: string, prefixes: Record<string, string>): JsonLdValue {
  const iri = resolveIri(term, prefixes);
  if (iri) return { "@id": iri };
  const lang = term.match(/^"(.*)"@([\w-]+)$/);
  if (lang) return { "@value": lang[1], "@language": lang[2] };
  const typed = term.match(/^"(.*)"\^\^<?([\w:/#.-]+)>?$/);
  if (typed) {
    const type = resolveIri(typed[2], prefixes) ?? typed[2];
    return { "@value": typed[1], "@type": type };
  }
  const plain = term.match(/^"(.*)"$/);
  if (plain) return { "@value": plain[1] };
  if (/^-?\d+(\.\d+)?$/.test(term) || /^(true|false)$/.test(term)) return { "@value": term };
  return { "@value": term };
}

function jsonLdToTerm(value: JsonLdValue | string | number | boolean, prefixes: Record<string, string>): string | undefined {
  if (value === null || typeof value !== "object") {
    const native = String(value);
    return /^-?\d+(\.\d+)?$/.test(native) || native === "true" || native === "false" ? native : `"${native}"`;
  }
  if (value["@id"]) return compactIri(value["@id"], prefixes);
  if (value["@value"] !== undefined) {
    if (value["@language"]) return `"${value["@value"]}"@${value["@language"]}`;
    if (value["@type"]) {
      const type = compactIri(value["@type"], prefixes);
      return `"${value["@value"]}"^^${type}`;
    }
    return `"${value["@value"]}"`;
  }
  return undefined;
}

/**
 * Quads -> expanded JSON-LD. Named graphs round-trip: quads carrying a
 * `graph` become JSON-LD named-graph entries ({"@id": graph, "@graph": ...}),
 * graph-less quads land in the default graph. Provenance fields
 * (source/rule) do not round-trip — JSON-LD carries the statements, the
 * store keeps the evidence.
 */
export function quadsToJsonLd(quads: Quad[], prefixes: Record<string, string> = JSONLD_PREFIXES): Record<string, unknown> {
  const byGraph = new Map<string, Quad[]>();
  for (const q of quads) {
    const g = q.graph ?? "";
    byGraph.set(g, [...(byGraph.get(g) ?? []), q]);
  }
  const buildNodes = (group: Quad[]): Record<string, unknown>[] => {
    const nodes = new Map<string, Record<string, unknown>>();
    const nodeOf = (id: string): Record<string, unknown> => {
      let n = nodes.get(id);
      if (!n) {
        n = { "@id": resolveIri(id, prefixes) ?? id };
        nodes.set(id, n);
      }
      return n;
    };
    for (const q of group) {
      const node = nodeOf(q.subject);
      if (q.predicate === "rdf:type" || q.predicate === "a") {
        const existing = node["@type"];
        const typeIri = resolveIri(q.object, prefixes) ?? q.object;
        node["@type"] = Array.isArray(existing) ? [...existing, typeIri].filter((t, i, a) => a.indexOf(t) === i) : existing ? [existing, typeIri] : typeIri;
        continue;
      }
      const key = resolveIri(q.predicate, prefixes) ?? q.predicate;
      const value = termToJsonLd(q.object, prefixes);
      const existing = node[key];
      node[key] = Array.isArray(existing) ? [...existing, value] : existing ? [existing, value] : value;
    }
    return [...nodes.values()];
  };
  const graphEntries: unknown[] = [];
  for (const [g, group] of byGraph) {
    const nodes = buildNodes(group);
    if (g === "") graphEntries.push(...nodes);
    else graphEntries.push({ "@id": resolveIri(g, prefixes) ?? g, "@graph": nodes });
  }
  return { "@context": prefixes, "@graph": graphEntries };
}

/** Expanded JSON-LD -> quads (named graphs preserved; prefixed terms restored where the map covers them). */
export function jsonldToQuads(
  doc: { "@graph"?: unknown[] } & Record<string, unknown>,
  prefixes: Record<string, string> = JSONLD_PREFIXES,
): { subject: string; predicate: string; object: string; graph?: string }[] {
  const out: { subject: string; predicate: string; object: string; graph?: string }[] = [];
  let blank = 0;
  const convertNode = (node: Record<string, unknown>, graphName: string | undefined): void => {
    const subjectId = (node["@id"] ? String(node["@id"]) : "") ? compactIri(String(node["@id"]), prefixes) : `_:b${blank++}`;
    const types = node["@type"];
    for (const t of Array.isArray(types) ? types : types ? [types] : []) {
      out.push({ subject: subjectId, predicate: "rdf:type", object: compactIri(String(t), prefixes), ...(graphName ? { graph: graphName } : {}) });
    }
    for (const [key, raw] of Object.entries(node)) {
      if (key === "@id" || key === "@type") continue;
      const predicate = compactIri(key.includes(":") ? resolveIri(key, prefixes) ?? key : key, prefixes);
      for (const v of Array.isArray(raw) ? raw : [raw]) {
        const object = jsonLdToTerm(v as JsonLdValue | string, prefixes);
        if (object !== undefined) out.push({ subject: subjectId, predicate, object, ...(graphName ? { graph: graphName } : {}) });
      }
    }
  };
  for (const entry of doc["@graph"] ?? []) {
    if (entry && typeof entry === "object" && Array.isArray((entry as Record<string, unknown>)["@graph"])) {
      const named = entry as { "@id"?: string; "@graph": Record<string, unknown>[] };
      const graphName = named["@id"] ? compactIri(named["@id"], prefixes) : `_:g${out.length}`;
      for (const node of named["@graph"]) convertNode(node, graphName);
      continue;
    }
    convertNode(entry as Record<string, unknown>, undefined);
  }
  return out;
}