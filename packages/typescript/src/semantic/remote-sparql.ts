import { z } from "zod";
import { AgentError } from "../types/index.js";
import { defineTool, type Tool } from "../tools/index.js";

/**
 * Remote SPARQL adapter (roadmap P2-sem): talk to real SPARQL endpoints —
 * Wikidata Query Service, Fuseki, GraphDB, Virtuoso — with auth and pinned
 * prefixes. The local GraphStore stays the read path for closures; this is
 * for data that lives OUTSIDE the agent's own graph.
 *
 * Protocol: SPARQL 1.1 — SELECT via POST `application/sparql-query`,
 * UPDATE via POST `application/sparql-update`; SELECT responses must be
 * `application/sparql-results+json` (we ask for it in Accept).
 */

export type SparqlAuth =
  | { type: "basic"; user: string; password: string }
  | { type: "bearer"; token: string }
  | { type: "header"; name: string; value: string };

export interface RemoteSparqlOptions {
  /** Query endpoint URL (e.g. https://query.wikidata.org/sparql). */
  endpoint: string;
  /** Update endpoint; defaults to `endpoint`. */
  updateEndpoint?: string;
  auth?: SparqlAuth;
  /** Pinned prefixes, prepended to every query as PREFIX declarations. */
  prefixes?: Record<string, string>;
  /** Extra request headers (merged over the defaults below). */
  headers?: Record<string, string>;
  fetchFn?: typeof fetch;
  /** Per-request timeout. Default 30s. */
  timeoutMs?: number;
  /**
   * Built-in retries for transient endpoints (429/5xx/network). Rate limits
   * are common on public SPARQL endpoints; the middleware retry is a separate
   * layer for MODEL calls only. Default 2. Honors Retry-After when present.
   */
  maxRetries?: number;
}

/** One SELECT result row: variable name -> plain string term. */
export type SparqlRow = Record<string, string>;

const DEFAULT_PREFIXES: Record<string, string> = {
  rdf: "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
  rdfs: "http://www.w3.org/2000/01/rdf-schema#",
  owl: "http://www.w3.org/2002/07/owl#",
  xsd: "http://www.w3.org/2001/XMLSchema#",
  skos: "http://www.w3.org/2004/02/skos/core#",
  dct: "http://purl.org/dc/terms/",
  prov: "http://www.w3.org/ns/prov#",
  schema: "https://schema.org/",
};

export class RemoteSparqlAdapter {
  readonly endpoint: string;
  private readonly updateEndpoint: string;
  private readonly prefixes: Record<string, string>;
  private readonly extraHeaders: Record<string, string>;
  private readonly fetchFn: typeof fetch;
  private readonly timeoutMs: number;
  private readonly maxRetries: number;
  private readonly auth?: SparqlAuth;

  constructor(opts: RemoteSparqlOptions) {
    this.endpoint = opts.endpoint.replace(/\/$/, "");
    this.updateEndpoint = (opts.updateEndpoint ?? opts.endpoint).replace(/\/$/, "");
    this.prefixes = { ...DEFAULT_PREFIXES, ...opts.prefixes };
    this.extraHeaders = opts.headers ?? {};
    this.fetchFn = opts.fetchFn ?? fetch;
    this.timeoutMs = opts.timeoutMs ?? 30000;
    this.maxRetries = Math.max(0, opts.maxRetries ?? 2);
    this.auth = opts.auth;
  }

  /** Prepend pinned PREFIX declarations so caller queries stay short. */
  withPrefixes(query: string): string {
    const declarations = Object.entries(this.prefixes)
      .map(([prefix, iri]) => `PREFIX ${prefix}: <${iri}>`)
      .join("\n");
    return `${declarations}\n${query}`;
  }

  private headers(extra: Record<string, string> = {}): Record<string, string> {
    const out: Record<string, string> = { ...extra, ...this.extraHeaders };
    // Public endpoints (Wikidata notably) REJECT requests without a
    // descriptive User-Agent with 403 — send one by default.
    out["user-agent"] ??= "agent-framework/0.1 (+RemoteSparqlAdapter)";
    if (!this.auth) return out;
    if (this.auth.type === "basic") {
      out.authorization = `Basic ${Buffer.from(`${this.auth.user}:${this.auth.password}`).toString("base64")}`;
    } else if (this.auth.type === "bearer") {
      out.authorization = `Bearer ${this.auth.token}`;
    } else {
      out[this.auth.name] = this.auth.value;
    }
    return out;
  }

  private async post(
    url: string,
    contentType: string,
    body: string,
    accept: string,
  ): Promise<Response> {
    let lastError: AgentError | undefined;
    let retryDelayMs: number | undefined;
    for (let attempt = 0; attempt <= this.maxRetries; attempt++) {
      if (attempt > 0) {
        // Public SPARQL endpoints rate-limit aggressively (429 with empty
        // bodies is common); honor Retry-After when the server sends it.
        await new Promise((r) => setTimeout(r, retryDelayMs ?? Math.min(2000 * 2 ** (attempt - 1), 10000)));
      }
      let res: Response;
      try {
        res = await this.fetchFn(url, {
          method: "POST",
          headers: this.headers({ "content-type": contentType, accept }),
          body,
          signal: AbortSignal.timeout(this.timeoutMs),
        });
      } catch (err) {
        lastError = new AgentError(
          "REMOTE_SPARQL",
          `SPARQL request failed: ${err instanceof Error ? err.message : String(err)}`,
          true,
        );
        retryDelayMs = undefined; // no server hint — retry loop uses its own backoff
        continue;
      }
      if (!res.ok) {
        const text = await res.text().catch(() => "");
        const retryable = res.status === 429 || res.status >= 500;
        const err = new AgentError(
          "REMOTE_SPARQL",
          `${url} HTTP ${res.status}: ${text.slice(0, 300)}`,
          retryable,
        );
        const retryAfterHeader = Number.parseInt(res.headers.get("retry-after") ?? "", 10);
        retryDelayMs = Number.isFinite(retryAfterHeader) ? Math.min(retryAfterHeader * 1000, 60000) : undefined;
        if (!retryable || attempt === this.maxRetries) throw err;
        lastError = err;
        continue;
      }
      return res;
    }
    // Unreachable for the non-ok path (throws above); a network error on the
    // final attempt lands here.
    throw lastError ?? new AgentError("REMOTE_SPARQL", "request failed", true);
  }

  /**
   * SELECT over the remote endpoint. Returns rows of plain-string terms
   * (same shape as the local `executeSelect`) so both path kinds compose.
   */
  async select(query: string): Promise<SparqlRow[]> {
    const res = await this.post(
      this.endpoint,
      "application/sparql-query",
      this.withPrefixes(query),
      "application/sparql-results+json",
    );
    let json: { head?: { vars?: string[] }; results?: { bindings?: Record<string, { value?: string }>[] } };
    try {
      json = (await res.json()) as typeof json;
    } catch (err) {
      // Server ignored Accept and sent something else (or truncated).
      throw new AgentError(
        "REMOTE_SPARQL",
        `Endpoint returned non-JSON results (expected sparql-results+json): ${err instanceof Error ? err.message : String(err)}`,
        false,
      );
    }
    const vars = json.head?.vars ?? [];
    const bindings = json.results?.bindings ?? [];
    return bindings.map((b) => {
      const row: SparqlRow = {};
      for (const v of vars) row[v] = b[v]?.value ?? "";
      return row;
    });
  }

  /**
   * SPARQL 1.1 UPDATE (INSERT/DELETE DATA, DELETE WHERE...) against the
   * update endpoint. Opt-in — never wire this into a read-only agent.
   */
  async update(query: string): Promise<void> {
    await this.post(
      this.updateEndpoint,
      "application/sparql-update",
      this.withPrefixes(query),
      "*/*",
    );
  }
}

/** Agent-facing tool. SELECT always; UPDATE only when `allowUpdate`. */
export function remoteSparqlTool(adapter: RemoteSparqlAdapter, opts: { allowUpdate?: boolean } = {}): Tool {
  if (!opts.allowUpdate) {
    return defineTool({
      name: "remote_sparql_select",
      description: "Query a remote SPARQL endpoint (read-only SELECT).",
      schema: z.object({ query: z.string().min(1).max(20000) }),
      execute: async ({ query }: { query: string }) => adapter.select(query),
    });
  }
  return defineTool({
    name: "remote_sparql",
    description: "Query or mutate a remote SPARQL endpoint (SELECT, or UPDATE with { update: true }).",
    schema: z.object({ query: z.string().min(1).max(20000), update: z.boolean().default(false) }),
    execute: async ({ query, update }: { query: string; update?: boolean }) => {
      if (update) {
        await adapter.update(query);
        return { updated: true };
      }
      return adapter.select(query);
    },
  });
}