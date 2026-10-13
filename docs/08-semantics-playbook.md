# 08 — Semantics Playbook (every way to use the graph)

The graph is not a database the agent reads. It is the agent's **ground truth
layer**: what the agent may claim, what it remembers across sessions, and what
proves its answers. Six uses, composed into one loop.

## 1. Autonomous validated writes (autonomy without anarchy)

Agents write freely — validation is mechanical, not human-gated:

```ts
import { InMemoryGraphStore, graphAddTool } from "framework-agents";

const store = new InMemoryGraphStore({ shapes: [personShape], rules: [basedInRule] });
const agent = new Agent({ model, tools: [graphAddTool(store), ...] });
```

`graph_add` runs provenance + SHACL + functional checks inside `store.add`.
Violations come back **as tool results, not throws**, so the model repairs and
retries on its own (see `STRUCTURED_*` retry semantics). Rules:
- Every quad needs `source` (who vouched). No source = rejected.
- Shape violations name the node + path (`ex:ada needs >= 1 ex:name`).
- Functional conflicts reject the whole write (rollback included).

## 2. Hallucination reduction (verify before asserting)

Three habits, cheapest first:

1. **Look up, don't invent.** `ontology_lookup("works for")` resolves the
   canonical predicate before the model coins `worksAt`. Unknown terms never
   enter the graph silently.
2. **Check claims.** `claim_check({ subject, predicate, object })` returns
   `grounded` (with `why()` proof) | `unknown` (do not assert) |
   `contradicted` (functional clash — investigate, don't overwrite).
3. **Read the closure, not the pile.** `sparql_select` queries explicit +
   inferred facts together, so answers cite completed knowledge.

Prompt pattern: *"Before stating any fact about a person or org, call
claim_check. If unknown, say what is missing instead of guessing."*

## 3. Session memory (what happened) + LTS (what is true)

```ts
const memory = new GraphMemory(store, new FileMemory("./runs/t-1"));
const agent = new Agent({ model, memory });
await memory.exportState();          // quads JSON → SessionManager snapshot
await memory2.importState(exported); // facts live again (inference recomputed)
```

- `load()` injects top-k closure facts relevant to the latest user turn, so
  the model reasons over completed knowledge without prompt stuffing.
- `save()` keeps the message log; triples enter only through validated
  `graph_add` (never silent parsing).
- `exportState` drops inferred quads (they recompute); `importState` restores
  explicit facts. Pair with `SessionManager` snapshots for time travel.

## 4. Reasoning with proofs (cite, don't vibe)

RDFS (subclass/type/subproperty/domain/range), OWL (transitive/symmetric/
inverse/functional), and versioned `CONSTRUCT`-style rules all run to fixpoint
on every `add`. Every inferred quad carries `rule` (`based-in@1`) and
`why()` returns premises. Answers should cite rule + source quads.

## 5. Governance (meaning stays stable while data churns)

See `07-semantics-governance.md`: foundation vocab (reuse) → domain TBox
(steward-owned, versioned) → per-agent shapes. `ontology_lookup` + SHACL
rejection together prevent ontology drift (`worksAt` vs `worksFor`).

## 6. The composed loop

```
lookup term → sparql_select (read closure) → reason (entailment is automatic)
  → claim_check (verify) → graph_add (validated write, self-repair on failure)
  → answer with proof → exportState (persist LTS)
```

Each step is a tool call the model makes on its own; each gate is mechanical.
That is the whole trick: the agent is fully autonomous inside guardrails that
are checked by code, not by humans.
