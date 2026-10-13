import { InMemoryGraphStore } from "../src/index.js";

const store = new InMemoryGraphStore();
await store.add([
  { subject: "ex:alice", predicate: "ex:worksFor", object: "ex:acme", source: "https://example.com/org" },
  { subject: "ex:acme", predicate: "ex:locatedIn", object: "ex:berlin", source: "https://example.com/org" },
]);

console.log("worksFor:", await store.match("ex:alice", "ex:worksFor", null));
// P1-sem: same query hits the entailment closure (basedIn via CONSTRUCT rule R1) + why() proof.
console.log(" entailment + SHACL + rules land per docs/04 P0-sem/P1-sem.");
