import { z } from "zod";
import { defineTool, type Tool } from "../tools/index.js";
import { tokenize } from "../skills/index.js";

/**
 * Retrieval interface — no bundled vector DB (local-first).
 * Adapters: InMemoryRetriever (TF-IDF, dependency-free), SparqlRetriever
 * over any GraphStore. retrievalTool() exposes them to agents.
 */

export interface ScoredDoc {
  id: string;
  text: string;
  score: number;
  meta?: Record<string, unknown>;
}

export interface Retriever {
  readonly id: string;
  search(query: string, k?: number): Promise<ScoredDoc[]>;
}

/** Dependency-free TF-IDF retriever over static documents. */
export class InMemoryRetriever implements Retriever {
  readonly id = "tfidf";
  private docs: { id: string; text: string; meta?: Record<string, unknown>; terms: Map<string, number> }[];
  private df = new Map<string, number>();

  constructor(docs: { id: string; text: string; meta?: Record<string, unknown> }[]) {
    this.docs = docs.map((d) => {
      const terms = new Map<string, number>();
      for (const t of tokenize(d.text)) terms.set(t, (terms.get(t) ?? 0) + 1);
      return { ...d, terms };
    });
    for (const d of this.docs) {
      for (const t of new Set(d.terms.keys())) this.df.set(t, (this.df.get(t) ?? 0) + 1);
    }
  }

  private idf(term: string): number {
    return Math.log((this.docs.length + 1) / ((this.df.get(term) ?? 0) + 1)) + 1;
  }

  async search(query: string, k = 4): Promise<ScoredDoc[]> {
    const qtf = new Map<string, number>();
    for (const t of tokenize(query)) qtf.set(t, (qtf.get(t) ?? 0) + 1);
    return this.docs
      .map((d) => {
        let dot = 0;
        let norm = 0;
        for (const [term, count] of d.terms) {
          const w = count * this.idf(term);
          norm += w * w;
          if (qtf.has(term)) dot += w * (qtf.get(term) ?? 0) * this.idf(term);
        }
        const qnorm = Math.sqrt([...qtf.values()].reduce((n, c) => n + c * c, 0)) || 1;
        return { id: d.id, text: d.text, meta: d.meta, score: dot / ((Math.sqrt(norm) || 1) * qnorm) };
      })
      .filter((d) => d.score > 0)
      .sort((a, b) => b.score - a.score)
      .slice(0, k);
  }
}

export function retrievalTool(retriever: Retriever, opts: { k?: number; name?: string } = {}) {
  return defineTool({
    name: opts.name ?? "retrieve",
    description: `Search the knowledge base (${retriever.id}). Returns top passages with scores.`,
    schema: z.object({ query: z.string().min(1).max(500), k: z.number().int().min(1).max(10).default(opts.k ?? 4) }),
    execute: async ({ query, k }: { query: string; k?: number }) => retriever.search(query, k),
  });
}

/** SPARQL-backed retriever: keyword match over the closure, rendered as text. */
export function sparqlRetriever(
  store: { match: (s: string | null, p: string | null, o: string | null) => Promise<{ subject: string; predicate: string; object: string }[]> },
  opts: { id?: string } = {},
): Retriever {
  return {
    id: opts.id ?? "sparql",
    search: async (query: string, k = 4) => {
      const terms = new Set(tokenize(query));
      if (!terms.size) return [];
      const quads = await store.match(null, null, null);
      return quads
        .map((q, i) => {
          const hay = `${q.subject} ${q.predicate} ${q.object}`.toLowerCase();
          let score = 0;
          for (const t of terms) if (hay.includes(t)) score += 1;
          return { id: `q${i}`, text: `${q.subject} ${q.predicate} ${q.object}`, score };
        })
        .filter((d) => d.score > 0)
        .sort((a, b) => b.score - a.score)
        .slice(0, k);
    },
  };
}

export type { Tool };
