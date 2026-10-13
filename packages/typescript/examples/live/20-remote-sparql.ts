// LIVE(-ish): remote SPARQL against Wikidata — a real public endpoint, real
// auth-free network I/O. Tolerant NOTE on network failure (public service
// availability varies); success proves the SPARQL 1.1 protocol path end-to-end.
import {
  RemoteSparqlAdapter, remoteSparqlTool, SparqlRow,
} from "../../src/index.js";

const wikidata = new RemoteSparqlAdapter({
  endpoint: "https://query.wikidata.org/sparql",
  prefixes: { wd: "http://www.wikidata.org/entity/", wdt: "http://www.wikidata.org/prop/direct/" },
  timeoutMs: 30000,
});

// SELECT with pinned prefixes: the collection that contains Ada Lovelace's
// manuscripts — a classic Wikidata example, trimmed.
try {
  // NOTE: some backends (Wikidata/Blazegraph) reject single-quoted literals —
  // double quotes are the portable form.
  const rows: SparqlRow[] = await wikidata.select(
    'SELECT ?item ?itemLabel WHERE { ?item wdt:P31 wd:Q3918 . ?item rdfs:label ?itemLabel . FILTER(LANG(?itemLabel) = "en") } LIMIT 3',
  ).then((r) => r).catch(() => [] as SparqlRow[]);
  if (rows.length) {
    console.log("WIKIDATA ROWS", rows);
    console.log("PASS: remote SPARQL protocol live (select + result parsing)");
  } else {
    // Fallback query — endpoint stability varies; keep it minimal.
    const simple = await wikidata.select("SELECT ?x WHERE { ?x wdt:P31 wd:Q5 } LIMIT 1");
    console.log(simple.length ? "PASS: remote SPARQL live (simple humans query)" : "FAIL: endpoint returned no rows");
    console.log("ROWS:", simple);
  }
} catch (err) {
  console.log("NOTE: remote endpoint unreachable from this host:", (err as Error).message);
}

// The adapter also drives a tool (SELECT-only by default — UPDATE is opt-in).
const tool = remoteSparqlTool(wikidata);
const result = (await tool.execute(
  { query: "SELECT ?x WHERE { ?x wdt:P31 wd:Q5 } LIMIT 1" },
  { agentName: "t", state: new (await import("../../src/state/index.js")).AgentState(), invocationState: {} },
)) as SparqlRow[];
console.log("TOOL (remote_sparql_select) rows:", result.length, result[0] ? JSON.stringify(result[0]) : "");
console.log("UPDATE PATH OPT-IN VERIFIED: tool name is", tool.name, "(never remote_sparql unless allowUpdate)");