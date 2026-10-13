"""Semantic graph layer. Parity: packages/typescript/src/semantic/index.ts +
entity-linking.ts + ontology-evolution.ts (merge into one module; ontology
review lives in ontology_review.py, remote SPARQL in remote_sparql.py).

In-memory store: RDFS-subset + OWL property axioms entailment with proofs,
SHACL-subset validation, versioned CONSTRUCT-style rules, SPARQL-subset SELECT
tools, entity linking + JSON-LD I/O, TBox evolution, and GraphMemory.

Pinned semantics (deviations from TS are deliberate, documented, and tested):

- Quad identity includes the named graph: the same s/p/o in two graphs are TWO
  quads.
- add() validates provenance BEFORE writing: a missing source is a violation,
  never a fact. Functional-property conflicts and SHACL violations are ERRORS
  with rollback — a conflict is never committed as a second fact.
- Literals are written in the COMPACT Turtle form ("x"^^xsd:date, "x"@en) — the
  TS store code uses single-quoted-less compact form with NO angle brackets;
  the term parsers accept both compact and bracketed datatypes.
- TBox migration writes the new version's axioms to the version-named graph
  ``urn:framework:ontology:<to>`` (TS uses a personal-name prefix
  a personal-name prefix — the Python port uses version-graph naming WITHOUT
  any personal name).
"""

from __future__ import annotations

import dataclasses
import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, Mapping, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from .memory import Memory
from .errors import AgentError
from .tools import Tool, ToolContext, ToolResult, define_tool
from .types import ChatMessage, assistant


# ---------------------------------------------------------------------------
# Terms, quads, literals
# ---------------------------------------------------------------------------

RDF_TYPE = {"rdf:type", "a", "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"}
SUB_CLASS = {"rdfs:subClassOf", "http://www.w3.org/2000/01/rdf-schema#subClassOf"}
SUB_PROP = {"rdfs:subPropertyOf", "http://www.w3.org/2000/01/rdf-schema#subPropertyOf"}
DOMAIN = {"rdfs:domain", "http://www.w3.org/2000/01/rdf-schema#domain"}
RANGE = {"rdfs:range", "http://www.w3.org/2000/01/rdf-schema#range"}
OWL_TRANSITIVE = {"owl:TransitiveProperty", "http://www.w3.org/2002/07/owl#TransitiveProperty"}
OWL_SYMMETRIC = {"owl:SymmetricProperty", "http://www.w3.org/2002/07/owl#SymmetricProperty"}
OWL_FUNCTIONAL = {"owl:FunctionalProperty", "http://www.w3.org/2002/07/owl#FunctionalProperty"}
OWL_INVERSE = {"owl:inverseOf", "http://www.w3.org/2002/07/owl#inverseOf"}


@dataclass
class Quad:
    """One statement. Identity is subject|predicate|object|graph."""

    subject: str
    predicate: str
    object: str
    graph: str | None = None
    #: Provenance: who vouched / source URI. Required on explicit writes.
    source: str | None = None
    derived_from: str | None = None
    rule: str | None = None


@dataclass
class Violation:
    message: str
    node: str | None = None
    path: str | None = None


@dataclass
class ShapeReport:
    valid: bool
    violations: list[Violation] = field(default_factory=list)


@dataclass
class Derivation:
    """Proof chain for an entailed fact: premises + rule id."""

    premises: list[Quad]
    rule: str


_LITERAL_TERM_RE = re.compile(r"^(-?\d+(\.\d+)?|true|false)$")
_TYPED_RE = re.compile(r"\^\^<?([\w:/#.-]+)>?$", re.ASCII)
_LANG_RE = re.compile(r'^".*"@[\w-]+$', re.ASCII)
_PLAIN_RE = re.compile(r'^".*"$')
_INT_RE = re.compile(r"^-?\d+$")
_DECIMAL_RE = re.compile(r"^-?\d*\.\d+$")
_BOOL_RE = re.compile(r"^(true|false)$")
_PREFIXED_RE = re.compile(r"^[a-zA-Z][\w-]*:", re.ASCII)


def is_literal(term: str) -> bool:
    """A term is a literal when quoted, numeric, boolean, or un-IRI-able.

    Mirrors the TS isLiteral: <iris> and prefixed names are IRIs; everything
    else (an un-resolvable bare word) is treated as a literal.
    """
    if term.startswith('"'):
        return True
    if _LITERAL_TERM_RE.match(term):
        return True
    if term.startswith("<") or _PREFIXED_RE.match(term):
        return False
    return True


def term_datatype(term: str) -> str | None:
    """Datatype of a literal term (typed / langString / string / integer /
    decimal / boolean). Accepts both ``"x"^^xsd:date`` and ``"x"^^<xsd:date>``."""
    m = _TYPED_RE.search(term)
    if m:
        return m.group(1)
    if _LANG_RE.match(term):
        return "rdf:langString"
    if _PLAIN_RE.match(term):
        return "xsd:string"
    if _INT_RE.match(term):
        return "xsd:integer"
    if _DECIMAL_RE.match(term):
        return "xsd:decimal"
    if _BOOL_RE.match(term):
        return "xsd:boolean"
    return None


@runtime_checkable
class GraphStore(Protocol):
    """Minimal quad-store contract — in-memory first, remote SPARQL later."""

    async def add(self, quads: list[Quad]) -> None: ...
    async def match(
        self,
        s: str | None = None,
        p: str | None = None,
        o: str | None = None,
        g: str | None = None,
    ) -> list[Quad]: ...
    async def validate(self, quads: list[Quad]) -> ShapeReport: ...
    async def why(self, quad: Quad) -> Derivation | None: ...


# ---------------------------------------------------------------------------
# SHACL-subset shapes + versioned CONSTRUCT-style rules
# ---------------------------------------------------------------------------


@dataclass
class PropertyShape:
    path: str
    min_count: int | None = None
    max_count: int | None = None
    datatype: str | None = None
    node_kind: Literal["iri", "literal"] | None = None
    has_value: str | None = None


@dataclass
class NodeShape:
    target_class: str
    properties: list[PropertyShape] = field(default_factory=list)


@dataclass
class TriplePattern:
    subject: str
    predicate: str
    object: str


@dataclass
class GraphRule:
    """Versioned CONSTRUCT-style rule over triple patterns (?vars)."""

    id: str
    where: list[TriplePattern] = field(default_factory=list)
    construct: list[TriplePattern] = field(default_factory=list)
    version: str | None = None


@dataclass
class StoreOptions:
    shapes: list[NodeShape] = field(default_factory=list)
    rules: list[GraphRule] = field(default_factory=list)


class InMemoryGraphStore:
    """Closure-entailing store: RDFS subset + OWL axioms + versioned rules."""

    def __init__(
        self,
        opts: StoreOptions | None = None,
        *,
        shapes: list[NodeShape] | None = None,
        rules: list[GraphRule] | None = None,
    ) -> None:
        if opts is not None:
            shapes = shapes if shapes is not None else opts.shapes
            rules = rules if rules is not None else opts.rules
        self.shapes: list[NodeShape] = list(shapes or [])
        self.rules: list[GraphRule] = list(rules or [])
        self.quads: list[Quad] = []
        self.derivations: dict[str, Derivation] = {}

    def add_shapes(self, *shapes: NodeShape) -> "InMemoryGraphStore":
        self.shapes.extend(shapes)
        return self

    def add_rules(self, *rules: GraphRule) -> "InMemoryGraphStore":
        self.rules.extend(rules)
        return self

    def ontology_shapes(self) -> list[NodeShape]:
        """Registered SHACL shapes (carried to new stores by TBox migrations)."""
        return list(self.shapes)

    def ontology_rules(self) -> list[GraphRule]:
        """Registered CONSTRUCT rules (versioned with the ontology they encode)."""
        return list(self.rules)

    def _key(self, q: Quad) -> str:
        return f"{q.subject}|{q.predicate}|{q.object}|{q.graph or ''}"

    async def add(self, quads: list[Quad]) -> None:
        report = await self.validate(quads)
        if not report.valid:
            raise ValueError(f"SHACL violation: {'; '.join(v.message for v in report.violations)}")
        known = {self._key(q) for q in self.quads}
        added_keys: list[str] = []
        for q in quads:
            k = self._key(q)
            if k not in known:
                known.add(k)
                added_keys.append(k)
                self.quads.append(q)

        def rollback() -> None:
            doomed = set(added_keys)
            self.quads = [q for q in self.quads if self._key(q) not in doomed]
            for k in doomed:
                self.derivations.pop(k, None)

        conflicts = self._check_functional()
        if conflicts:
            rollback()

            raise ValueError(f"Functional property violation: {'; '.join(conflicts)}")
        shape_report = self.check_shapes(self.quads)
        if not shape_report.valid:
            rollback()

            raise ValueError(f"SHACL violation: {'; '.join(v.message for v in shape_report.violations)}")
        self._entail_to_fixpoint(known)

    async def match(
        self,
        s: str | None = None,
        p: str | None = None,
        o: str | None = None,
        g: str | None = None,
    ) -> list[Quad]:
        """Match by pattern; None = wildcard (including the graph slot).

        Queries the entailment closure. A ``g`` value selects ONE named graph
        — the same s/p/o CAN live in several graphs; identity includes it.
        """

        def keep(q: Quad) -> bool:
            return (
                (not s or q.subject == s)
                and (not p or q.predicate == p)
                and (not o or q.object == o)
                and (not g or q.graph == g)
            )

        return [q for q in self.quads if keep(q)]

    async def validate(self, quads: list[Quad]) -> ShapeReport:
        """Structural + provenance check: incomplete triples and source-less
        writes are violations — a fact without provenance never enters."""
        violations = [
            Violation(message=f"Incomplete triple: {json.dumps(_quad_json(q))}")
            for q in quads
            if not q.subject or not q.predicate or not q.object
        ]
        no_source = [
            Violation(message=f"Missing provenance (source) on {q.subject} {q.predicate} {q.object}")
            for q in quads
            if not q.source
        ]
        return ShapeReport(valid=not (violations or no_source), violations=[*violations, *no_source])

    async def why(self, quad: Quad) -> Derivation | None:
        """Proof chain for an entailed fact: premises + rule id. None for explicit facts."""
        return self.derivations.get(self._key(quad))

    def _check_functional(self) -> list[str]:
        """Functional properties: at most one value per subject (a conflict is
        an ERROR rolled back, never a second fact)."""
        functional_props = {
            q.subject for q in self.quads if q.predicate in RDF_TYPE and q.object in OWL_FUNCTIONAL
        }
        conflicts: list[str] = []
        for prop in functional_props:
            by_subject: dict[str, set[str]] = {}
            for q in self.quads:
                if q.predicate != prop:
                    continue
                by_subject.setdefault(q.subject, set()).add(q.object)
            for subject, values in by_subject.items():
                if len(values) > 1:
                    conflicts.append(f"{subject} has {len(values)} values for functional {prop}")
        return conflicts

    def check_shapes(self, quads: list[Quad]) -> ShapeReport:
        """SHACL-subset validation over a quad set (pure — no entailment)."""
        violations: list[Violation] = []
        for shape in self.shapes:
            targets = [q.subject for q in quads if q.predicate in RDF_TYPE and q.object == shape.target_class]
            for node in dict.fromkeys(targets):
                for prop in shape.properties:
                    values = [q.object for q in quads if q.subject == node and q.predicate == prop.path]
                    if prop.min_count is not None and len(values) < prop.min_count:
                        violations.append(
                            Violation(f"{node} needs >= {prop.min_count} {prop.path}", node, prop.path)
                        )
                    if prop.max_count is not None and len(values) > prop.max_count:
                        violations.append(
                            Violation(f"{node} allows <= {prop.max_count} {prop.path}", node, prop.path)
                        )
                    for v in values:
                        if prop.datatype and term_datatype(v) != prop.datatype and not (
                            prop.datatype == "xsd:string" and is_literal(v)
                        ):
                            violations.append(
                                Violation(f"{node} {prop.path}={v} is not {prop.datatype}", node, prop.path)
                            )
                        if prop.node_kind == "iri" and is_literal(v):
                            violations.append(
                                Violation(f"{node} {prop.path}={v} must be an IRI", node, prop.path)
                            )
                        if prop.node_kind == "literal" and not is_literal(v):
                            violations.append(
                                Violation(f"{node} {prop.path}={v} must be a literal", node, prop.path)
                            )
                        if prop.has_value is not None and v != prop.has_value:
                            violations.append(
                                Violation(f"{node} {prop.path} must be {prop.has_value}", node, prop.path)
                            )
        return ShapeReport(valid=not violations, violations=violations)

    def _insert(self, q: Quad, premises: list[Quad], rule: str, known: set[str]) -> bool:
        k = self._key(q)
        if k in known:
            return False
        known.add(k)
        self.quads.append(q)
        self.derivations[k] = Derivation(premises=list(premises), rule=rule)
        return True

    def _entail_to_fixpoint(self, known: set[str]) -> None:
        """RDFS subset + OWL property axioms + versioned rules, to fixpoint:

        - RDFS-SUB-TRANS / RDFS-TYPE / RDFS-SUBPROP-TRANS / RDFS-SUBPROP /
          RDFS-DOMAIN / RDFS-RANGE
        - OWL-TRANSITIVE / OWL-SYMMETRIC / OWL-INVERSE (inverseOf both directions)
        - ``<ruleId>@<version>`` for each applied GraphRule (premises -> why())
        """
        while True:
            added = False
            snapshot = list(self.quads)

            def by_pred(ps: set[str]) -> list[Quad]:
                return [q for q in snapshot if q.predicate in ps]

            def insert(q: Quad, premises: list[Quad], rule: str) -> bool:
                nonlocal added
                one = self._insert(q, premises, rule, known)
                added = one or added
                return one

            for ab in by_pred(SUB_CLASS):
                for bc in [q for q in by_pred(SUB_CLASS) if q.subject == ab.object]:
                    added = (
                        self._insert(
                            Quad(ab.subject, ab.predicate, bc.object, source="entailment", rule="RDFS-SUB-TRANS"),
                            [ab, bc],
                            "RDFS-SUB-TRANS",
                            known,
                        )
                        or added
                    )
            for t in by_pred(RDF_TYPE):
                for sc in [q for q in by_pred(SUB_CLASS) if q.subject == t.object]:
                    added = (
                        self._insert(
                            Quad(t.subject, t.predicate, sc.object, source="entailment", rule="RDFS-TYPE"),
                            [t, sc],
                            "RDFS-TYPE",
                            known,
                        )
                        or added
                    )
            for ab in by_pred(SUB_PROP):
                for bc in [q for q in by_pred(SUB_PROP) if q.subject == ab.object]:
                    added = (
                        self._insert(
                            Quad(
                                ab.subject, ab.predicate, bc.object, source="entailment", rule="RDFS-SUBPROP-TRANS"
                            ),
                            [ab, bc],
                            "RDFS-SUBPROP-TRANS",
                            known,
                        )
                        or added
                    )
                for use in [q for q in snapshot if q.predicate == ab.subject]:
                    added = (
                        self._insert(
                            Quad(ab.subject, ab.object, use.object, source="entailment", rule="RDFS-SUBPROP"),
                            [ab, use],
                            "RDFS-SUBPROP",
                            known,
                        )
                        or added
                    )
            for d in by_pred(DOMAIN):
                for use in [q for q in snapshot if q.predicate == d.subject]:
                    added = (
                        self._insert(
                            Quad(use.subject, "rdf:type", d.object, source="entailment", rule="RDFS-DOMAIN"),
                            [d, use],
                            "RDFS-DOMAIN",
                            known,
                        )
                        or added
                    )
            for r in by_pred(RANGE):
                for use in [q for q in snapshot if q.predicate == r.subject]:
                    added = (
                        self._insert(
                            Quad(use.object, "rdf:type", r.object, source="entailment", rule="RDFS-RANGE"),
                            [r, use],
                            "RDFS-RANGE",
                            known,
                        )
                        or added
                    )
            added = self._apply_owl(snapshot, insert) or added
            added = self._apply_rules(snapshot, insert) or added
            if not added:
                return

    def _apply_owl(self, snapshot: list[Quad], insert: Any) -> bool:
        added = False
        transitive = {
            q.subject for q in snapshot if q.predicate in RDF_TYPE and q.object in OWL_TRANSITIVE
        }
        symmetric = {q.subject for q in snapshot if q.predicate in RDF_TYPE and q.object in OWL_SYMMETRIC}
        for p in transitive:
            uses = [q for q in snapshot if q.predicate == p]
            for xy in uses:
                for yz in [q for q in uses if q.subject == xy.object]:
                    if insert(
                        Quad(xy.subject, p, yz.object, source="entailment", rule="OWL-TRANSITIVE"),
                        [xy, yz],
                        "OWL-TRANSITIVE",
                    ):
                        added = True
        for p in symmetric:
            for xy in [q for q in snapshot if q.predicate == p]:
                if insert(
                    Quad(xy.object, p, xy.subject, source="entailment", rule="OWL-SYMMETRIC"),
                    [xy],
                    "OWL-SYMMETRIC",
                ):
                    added = True
        for inv in [q for q in snapshot if q.predicate in OWL_INVERSE]:
            for use in [q for q in snapshot if q.predicate == inv.subject or q.predicate == inv.object]:
                flipped = inv.object if use.predicate == inv.subject else inv.subject
                if insert(
                    Quad(use.object, flipped, use.subject, source="entailment", rule="OWL-INVERSE"),
                    [inv, use],
                    "OWL-INVERSE",
                ):
                    added = True
        return added

    def _apply_rules(self, snapshot: list[Quad], insert: Any) -> bool:
        added = False
        for rule in self.rules:
            tag = f"{rule.id}@{rule.version}" if rule.version else rule.id
            for binding in match_patterns(rule.where, snapshot):
                for pattern in rule.construct:
                    built = TriplePattern(
                        instantiate(pattern.subject, binding.vars),
                        instantiate(pattern.predicate, binding.vars),
                        instantiate(pattern.object, binding.vars),
                    )
                    if not built.subject or not built.predicate or not built.object:
                        continue
                    if insert(
                        Quad(
                            built.subject,
                            built.predicate,
                            built.object,
                            source="entailment",
                            rule=tag,
                            derived_from=tag,
                        ),
                        binding.quads,
                        tag,
                    ):
                        added = True
        return added


def _quad_json(q: Quad) -> dict[str, Any]:
    out = dataclasses.asdict(q)
    return {k: v for k, v in out.items() if v is not None}


# ---------------------------------------------------------------------------
# Pattern matching + SPARQL-subset (SELECT with conjunctive triple patterns)
# ---------------------------------------------------------------------------


@dataclass
class PatternBinding:
    vars: dict[str, str]
    quads: list[Quad]


def _is_var(term: str) -> bool:
    return term.startswith("?")


def _match_term(pattern: str, value: str, vars: dict[str, str]) -> dict[str, str] | None:
    if _is_var(pattern):
        bound = vars.get(pattern)
        if bound is None:
            nxt = dict(vars)
            nxt[pattern] = value
            return nxt
        return vars if bound == value else None
    return vars if pattern == value else None


def _match_one(pattern: TriplePattern, quad: Quad, vars: dict[str, str]) -> dict[str, str] | None:
    v = _match_term(pattern.subject, quad.subject, vars)
    if v is None:
        return None
    v = _match_term(pattern.predicate, quad.predicate, v)
    if v is None:
        return None
    return _match_term(pattern.object, quad.object, v)


def match_patterns(patterns: list[TriplePattern], quads: list[Quad]) -> list[PatternBinding]:
    """Conjunctive matching of patterns against quads (nested-loop join)."""
    bindings: list[PatternBinding] = [PatternBinding(vars={}, quads=[])]
    for pattern in patterns:
        nxt: list[PatternBinding] = []
        for b in bindings:
            for q in quads:
                vars = _match_one(pattern, q, b.vars)
                if vars is not None:
                    nxt.append(PatternBinding(vars=vars, quads=[*b.quads, q]))
        bindings = nxt
        if not bindings:
            break
    return bindings


def instantiate(term: str, vars: dict[str, str]) -> str:
    return vars.get(term, term) if _is_var(term) else term


@dataclass
class SparqlSelect:
    variables: list[str]
    patterns: list[TriplePattern]


_TOKEN_RE = re.compile(r'"[^"]*"(?:\^\^<?[\w:/#.-]+>?|@[\w-]+)?|<[^>]*>|[{}().;]|\S+', re.ASCII)


def _tokenize_sparql(text: str) -> list[str]:
    return [m.group(0) for m in _TOKEN_RE.finditer(text)]


def parse_select(query: str) -> SparqlSelect:
    """Minimal SELECT parser: ``SELECT ?a ?b WHERE { s p o . ... }``.

    Terms: ?vars, <iris>, prefixed names, "literals" (^^datatype/@lang ok).
    Anything else raises ValueError — this is a documented subset, not SPARQL.
    """
    tokens = _tokenize_sparql(query)
    i = 0

    def expect(want: str) -> None:
        nonlocal i
        got = tokens[i] if i < len(tokens) else "end"
        if got.upper() != want:
            raise ValueError(f"SPARQL: expected {want}, got {got}")
        i += 1

    expect("SELECT")
    variables: list[str] = []
    while i < len(tokens) and tokens[i].startswith("?"):
        variables.append(tokens[i])
        i += 1
    if not variables:
        raise ValueError("SPARQL: SELECT needs at least one variable")
    expect("WHERE")
    expect("{")
    patterns: list[TriplePattern] = []
    while i < len(tokens) and tokens[i] != "}":
        s = tokens[i]
        i += 1
        p = tokens[i] if i < len(tokens) else None
        i += 1
        o = tokens[i] if i < len(tokens) else None
        i += 1
        if s is None or p is None or o is None or s == "." or p == "." or o == ".":
            raise ValueError("SPARQL: malformed triple pattern")
        patterns.append(TriplePattern(s, p, o))
        if i < len(tokens) and tokens[i] == ".":
            i += 1
    expect("}")
    return SparqlSelect(variables=variables, patterns=patterns)


async def execute_select(store: GraphStore, query: str) -> list[dict[str, str]]:
    """Execute a SELECT against the store closure. Returns variable bindings."""
    parsed = parse_select(query)
    quads = await store.match()
    return [
        {v: b.vars.get(v, "") for v in parsed.variables}
        for b in match_patterns(parsed.patterns, quads)
    ]


# ---------------------------------------------------------------------------
# Agent tools: read, write (autonomous, validated), verify
# ---------------------------------------------------------------------------


class _SparqlSelectArgs(BaseModel):
    query: str = Field(min_length=1, max_length=4000)


def sparql_select_tool(store: GraphStore) -> Tool:
    """Read-only SPARQL SELECT over the entailment closure."""

    async def execute(args: _SparqlSelectArgs, ctx: ToolContext | None):
        # Same contract as graph_add: query errors are repair feedback (the
        # model fixes the query), not a thrown tool error that kills the run.
        try:
            return await execute_select(store, args.query)
        except AgentError:
            raise
        except Exception as err:  # noqa: BLE001 - documented feedback contract
            return f"Query error — fix the syntax and retry: {err}"

    return define_tool(
        name="sparql_select",
        description="Query the knowledge graph. Subset: SELECT ?vars WHERE { s p o . ... } over the inferred closure.",
        schema=_SparqlSelectArgs,
        execute=execute,
    )


class _OntologyLookupArgs(BaseModel):
    term: str = Field(min_length=1, max_length=200)
    limit: int = Field(default=8, ge=1, le=20)


def ontology_lookup_tool(store: GraphStore) -> Tool:
    """Ontology lookup: find classes/properties/labels matching a term."""

    async def execute(args: _OntologyLookupArgs, ctx: ToolContext | None) -> list[dict[str, str]]:
        needle = args.term.lower()
        quads = await store.match()
        return [
            {"subject": q.subject, "predicate": q.predicate, "object": q.object}
            for q in quads
            if needle in f"{q.subject} {q.predicate} {q.object}".lower()
        ][: args.limit]

    return define_tool(
        name="ontology_lookup",
        description="Look up ontology terms (classes, properties, labels) instead of inventing predicates.",
        schema=_OntologyLookupArgs,
        execute=execute,
    )


class _TurtleQuadModel(BaseModel):
    subject: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    object: str = Field(min_length=1)
    source: str = Field(min_length=1, description="Provenance URI (required — writes without it are rejected)")


class _GraphAddArgs(BaseModel):
    quads: list[_TurtleQuadModel] = Field(min_length=1, max_length=50)


async def _graph_add_to_result(output: Any) -> ToolResult:
    """Validation failures are model-visible repair feedback, not hidden artifacts."""
    if isinstance(output, str):
        return ToolResult(content=output)
    return ToolResult(content=json.dumps(output), artifact={"committed": output["committed"]})


def graph_add_tool(store: GraphStore) -> Tool:
    """Autonomous validated write: SHACL + provenance + functional checks run
    inside the store. Violations return as tool results (not raises) so the
    model repairs and retries on its own — autonomy without anarchy."""

    async def execute(args: _GraphAddArgs, ctx: ToolContext | None) -> Any:
        try:
            await store.add([Quad(q.subject, q.predicate, q.object, source=q.source) for q in args.quads])
            inferred = [q for q in await store.match() if q.source == "entailment"]
            return {"committed": len(args.quads), "inferred": len(inferred)}
        except Exception as err:  # noqa: BLE001 - feedback contract
            return f"Validation failed — fix and retry: {err}"

    return define_tool(
        name="graph_add",
        description="Write facts to the knowledge graph. Validated (SHACL/provenance/functional); violations return repair feedback.",
        schema=_GraphAddArgs,
        execute=execute,
        to_result=_graph_add_to_result,
    )


ClaimVerdict = Literal["grounded", "unknown", "contradicted"]


class _ClaimCheckArgs(BaseModel):
    subject: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    object: str = Field(min_length=1)


def claim_check_tool(store: GraphStore) -> Tool:
    """Anti-hallucination primitive: is this triple in the closure (grounded),
    absent (unknown — do not assert), or clashing with a functional value
    (contradicted)? Ask before answering from memory."""

    async def execute(args: _ClaimCheckArgs, ctx: ToolContext | None) -> dict[str, Any]:
        direct = await store.match(args.subject, args.predicate, args.object)
        if direct:
            why = await store.why(direct[0])
            return {
                "verdict": "grounded",
                "proof": None
                if why is None
                else {
                    "premises": [_quad_json(p) for p in why.premises],
                    "rule": why.rule,
                },
            }
        siblings = await store.match(args.subject, args.predicate, None)
        declarations = await store.match(args.predicate, None, None)
        functional = any(q.predicate in RDF_TYPE and q.object in OWL_FUNCTIONAL for q in declarations)
        clash = [q for q in siblings if q.object != args.object]
        if functional and clash:
            return {"verdict": "contradicted", "proof": {"existing": [q.object for q in clash]}}
        return {
            "verdict": "unknown",
            "proof": {"related": [q.object for q in siblings]} if siblings else None,
        }

    return define_tool(
        name="claim_check",
        description="Verify a fact against the graph before asserting it. Returns grounded | unknown | contradicted with proof.",
        schema=_ClaimCheckArgs,
        execute=execute,
    )


# ---------------------------------------------------------------------------
# GraphMemory: closure-aware session memory + long-term store
# ---------------------------------------------------------------------------


def snake_tokenize(text: str) -> list[str]:
    """Local keyword tokenizer for the GraphMemory fact selector (TS skills tokenize)."""
    return [w for w in re.split(r"[^a-z0-9]+", text.lower()) if len(w) > 1]


@dataclass
class GraphMemory:
    """Session memory + long-term store in one:

    - load(): recent messages + top-k closure facts relevant to the latest user
      turn (keyword overlap), rendered as a knowledge message.
    - save(): delegates to inner memory (extraction of triples from prose stays
      model-driven via graph_add — never silent parsing).
    """

    store: GraphStore
    inner: Memory
    facts: int = 8

    async def load(self) -> list[ChatMessage]:
        messages = await self.inner.load()
        last_user = next((m for m in reversed(messages) if m.role == "user"), None)
        if last_user is None:
            return messages
        content = last_user.content if isinstance(last_user.content, str) else ""
        terms = set(snake_tokenize(content))
        if not terms:
            return messages
        quads = await self.store.match()
        scored = [
            (q, score)
            for q, score in (
                (q, sum(1 for t in terms if t in f"{q.subject} {q.predicate} {q.object}".lower()))
                for q in quads
            )
            if score > 0
        ]
        if not scored:
            return messages
        scored.sort(key=lambda item: -item[1])
        top = scored[: self.facts]
        lines = "\n".join(f"- {q.subject} {q.predicate} {q.object}" for q, _ in top)
        return [
            *messages,
            assistant(f"Relevant known facts (ground answers in these):\n{lines}"),
        ]

    async def save(self, messages: list[ChatMessage]) -> None:
        await self.inner.save(messages)

    async def clear(self) -> None:
        await self.inner.clear()

    async def export_state(self) -> dict[str, list[Quad]]:
        """Serialize the closure for snapshots / long-term persistence."""
        return {"quads": await self.store.match()}

    async def import_state(self, state: Mapping[str, Any] | Any) -> None:
        """Restore a previously exported closure (facts become explicit again)."""
        raw = state.get("quads", []) if hasattr(state, "get") else []
        fresh: list[Quad] = []
        for q in raw:
            qq = q if isinstance(q, Quad) else Quad(**q)
            if qq.source != "entailment":
                fresh.append(qq)
        if fresh:
            await self.store.add(fresh)


# ---------------------------------------------------------------------------
# Entity linking + sameAs canonicalization (docs/07 rule 2)
# ---------------------------------------------------------------------------

LABEL_PREDICATES = {
    "rdfs:label",
    "http://www.w3.org/2000/01/rdf-schema#label",
    "skos:prefLabel",
    "http://www.w3.org/2004/02/skos/core#prefLabel",
    "skos:altLabel",
    "http://www.w3.org/2004/02/skos/core#altLabel",
}
SAME_AS = {"owl:sameAs", "http://www.w3.org/2002/07/owl#sameAs"}

#: Prefixes to expand/compact IRIs for JSON-LD I/O (nullable values pass through).
JSONLD_PREFIXES: dict[str, str] = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "owl": "http://www.w3.org/2002/07/owl#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
    "skos": "http://www.w3.org/2004/02/skos/core#",
    "dct": "http://purl.org/dc/terms/",
    "prov": "http://www.w3.org/ns/prov#",
    "schema": "https://schema.org/",
    "ex": "urn:example:",
}


@dataclass
class LinkedEntity:
    #: Canonical (sameAs-group representative) IRI, in the store's term form.
    iri: str
    label: str | None = None
    score: float = 0
    #: Other IRIs the same entity is known as (the sameAs group).
    same_as_group: list[str] = field(default_factory=list)


def strip_literal(term: str) -> str:
    m = re.match(r'^"(.*)"(?:\^\^<?[\w:/#.-]+>?|@[\w-]+)?$', term, re.ASCII)
    return m.group(1) if m else term


def _local_name(iri: str) -> str:
    tail = re.split(r"[#/:]", iri)[-1] if iri else ""
    return tail.lower()


class EntityLinker:
    """Mention -> graph IRIs over the store closure, canonicalized through
    owl:sameAs groups — a mention linked through any alias resolves to ONE
    representative IRI."""

    def __init__(self, store: GraphStore, label_predicates: Iterable[str] | None = None) -> None:
        self.store = store
        self.label_predicates = set(label_predicates) if label_predicates is not None else set(LABEL_PREDICATES)

    async def _same_as_groups(self) -> dict[str, str]:
        """Union-find over owl:sameAs pairs: every alias cluster -> one representative."""
        pairs = [q for q in await self.store.match() if q.predicate in SAME_AS]
        parent: dict[str, str] = {}

        def find(x: str) -> str:
            root = x
            while root in parent and parent[root] != root:
                root = parent[root]
            return root

        def union(a: str, b: str) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for q in pairs:
            parent.setdefault(q.subject, q.subject)
            parent.setdefault(q.object, q.object)
            union(q.subject, q.object)
        return {member: find(member) for member in list(parent.keys())}

    async def link(self, mention: str, k: int = 5) -> list[LinkedEntity]:
        """Mention -> candidate entities, best first, canonicalized via sameAs."""
        needle = mention.strip().lower()
        if not needle:
            return []
        quads = await self.store.match()
        canonical = await self._same_as_groups()
        labels: dict[str, str] = {}
        label_score: dict[str, int] = {}

        candidates: list[dict[str, Any]] = []
        for q in quads:
            local_part = _local_name(q.subject)
            if "/" in q.subject or "#" in q.subject:
                full_tail = re.split(r"[#/]", q.subject)[-1] or ""
            else:
                full_tail = local_part
            if local_part == needle:
                candidates.append({"iri": q.subject, "label": None, "score": 40})
            elif local_part.startswith(needle):
                candidates.append({"iri": q.subject, "label": None, "score": 25})
            elif full_tail.lower() == needle:
                candidates.append({"iri": q.subject, "label": None, "score": 40})

            if q.predicate in self.label_predicates:
                label = strip_literal(q.object)
                low = label.lower()
                trust = 2 if "altLabel" in q.predicate else (0 if "prefLabel" in q.predicate else 1)
                if low == needle:
                    candidates.append({"iri": q.subject, "label": label, "score": 100})
                elif low.startswith(needle):
                    candidates.append({"iri": q.subject, "label": label, "score": 60})
                elif needle in low:
                    candidates.append({"iri": q.subject, "label": label, "score": 30})
                prev = label_score.get(q.subject)
                if prev is None or trust < prev:
                    labels[q.subject] = label
                    label_score[q.subject] = trust

        merged: dict[str, LinkedEntity] = {}
        for c in candidates:
            rep = canonical.get(c["iri"], c["iri"])
            existing = merged.get(rep)
            if existing is not None:
                existing.score = max(existing.score, c["score"])
                if c["label"] and not existing.label:
                    existing.label = c["label"]
                if c["iri"] not in existing.same_as_group:
                    existing.same_as_group.append(c["iri"])
            else:
                merged[rep] = LinkedEntity(
                    iri=rep,
                    label=c["label"] or labels.get(rep),
                    score=c["score"],
                    same_as_group=[c["iri"], *([rep] if rep != c["iri"] else [])],
                )
        for group in merged.values():
            if group.label is None:
                group.label = labels.get(group.iri)
            if len(group.same_as_group) < 2:
                members = [m for m in canonical if canonical[m] == canonical.get(group.iri)]
                if len(members) > 1:
                    group.same_as_group = members
        ranked = sorted(merged.values(), key=lambda e: -e.score)
        return ranked[:k]


class _LinkEntityArgs(BaseModel):
    mention: str = Field(min_length=1, max_length=200)
    k: int = Field(default=5, ge=1, le=10)


def link_entity_tool(linker: EntityLinker) -> Tool:
    """link_entity agent tool: mention -> canonical IRIs with scores."""

    async def execute(args: _LinkEntityArgs, ctx: ToolContext | None) -> list[dict[str, Any]]:
        return [dataclasses.asdict(e) for e in await linker.link(args.mention, args.k)]

    return define_tool(
        name="link_entity",
        description="Link a text mention to known graph entities (labels + sameAs canonicalized). Returns canonical IRIs with scores — never invent IRIs.",
        schema=_LinkEntityArgs,
        execute=execute,
    )


# ---------------------------------------------------------------------------
# Mapping proposals: LLM proposes; autonomy commits by default, review opts out
# ---------------------------------------------------------------------------

MappingDecision = Literal["approved", "rejected"]


@dataclass
class MappingProposal:
    id: str
    #: The source-side term: usually a raw column header / mention string.
    from_: str
    #: The proposed target: a canonical ontology IRI.
    to: str
    #: Provenance for the eventual commit (required by GraphStore.validate).
    source: str
    status: str = "pending"
    #: True when autonomy committed it on proposal (no human turn).
    committed_by_autonomy: bool | None = None
    proposed_at: str = ""


@dataclass
class MappingProposals:
    """Autonomy-first mapping commits. Autonomy: proposals commit IMMEDIATELY on
    proposal (default ON — the agent's whole write path is autonomous). Set
    ``autonomy=False`` (or bind no store) to queue proposals for decide()."""

    store: "GraphStore | None" = None
    autonomy: bool = True

    def __post_init__(self) -> None:
        self.seq = 0  # per-instance sequence, like the TS private seq
        self.items: dict[str, MappingProposal] = {}

    @property
    def is_autonomous(self) -> bool:
        """True when proposals commit themselves (autonomy on AND a store bound)."""
        return self.autonomy and self.store is not None

    async def _commit(self, store: GraphStore, proposal: MappingProposal) -> None:
        # Promotion = a provenance-carrying alias the linker can see forever.
        await store.add(
            [
                Quad(
                    proposal.to,
                    "skos:altLabel",
                    f'"{proposal.from_}"',
                    source=proposal.source,
                    derived_from=f"mapping:{proposal.id}",
                )
            ]
        )

    async def propose(self, from_: str, to: str, source: str) -> MappingProposal:
        """Record an LLM-proposed mapping. With autonomy (default) + a bound
        store it commits right away, provenance required; with autonomy=False
        (or no store bound) it queues as pending until decide()."""
        existing = next(
            (
                p
                for p in self.items.values()
                if p.from_ == from_
                and p.to == to
                and (p.status == "pending" or (self.is_autonomous and p.status == "approved"))
            ),
            None,
        )
        if existing is not None:
            return existing
        self.seq += 1
        proposal = MappingProposal(
            id=f"map-{self.seq}",
            from_=from_,
            to=to,
            source=source,
            status="pending",
        )
        proposal.proposed_at = _now_iso()
        if self.is_autonomous and self.store is not None:
            await self._commit(self.store, proposal)
            proposal.status = "approved"
            proposal.committed_by_autonomy = True
        self.items[proposal.id] = proposal
        return proposal

    def list(self, status: str | None = None) -> list[MappingProposal]:
        return [p for p in self.items.values() if status is None or p.status == status]

    async def decide(self, store: GraphStore, id: str, decision: MappingDecision) -> MappingProposal | None:
        """Human decision. ``approved`` also commits the alias into the graph."""
        proposal = self.items.get(id)
        if proposal is None or proposal.status != "pending":
            return None
        proposal.status = decision
        if decision == "approved":
            await self._commit(store, proposal)
        return proposal


class _ProposeMappingArgs(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(min_length=1, max_length=200, alias="from")
    to: str = Field(min_length=1, max_length=500)
    source: str = Field(min_length=1, description="Provenance URI for the committed alias")


def propose_mapping_tool(proposals: MappingProposals) -> Tool:
    """Autonomy mode commits the alias immediately (provenance required);
    review mode queues it for human approval. Validation failures are repair
    feedback, not a thrown tool error that would kill the run."""

    async def execute(args: _ProposeMappingArgs, ctx: ToolContext | None) -> Any:
        try:
            p = await proposals.propose(args.from_, args.to, args.source)
        except Exception as err:  # noqa: BLE001 - feedback contract
            return f"Commit rejected — fix and retry: {err}"
        if p.committed_by_autonomy:
            return {"id": p.id, "status": "approved", "committed": True}
        return {"id": p.id, "status": "pending", "note": "Review mode: awaits a human decide() before writing."}

    return define_tool(
        name="propose_mapping",
        description="Map a raw term to a canonical ontology IRI. Autonomy mode commits the alias immediately (provenance required); review mode queues it for human approval.",
        schema=_ProposeMappingArgs,
        execute=execute,
    )


# ---------------------------------------------------------------------------
# JSON-LD expand/compact I/O
# ---------------------------------------------------------------------------

_IRI_PREFIX_RE = re.compile(r"^([a-zA-Z][\w-]*):(.+)$", re.ASCII)
_HTTP_RE = re.compile(r"^https?://")


def _resolve_iri(term: str, prefixes: Mapping[str, str]) -> str | None:
    m = _IRI_PREFIX_RE.match(term)
    base = prefixes.get(m.group(1)) if m else None
    if m and base:
        return f"{base}{m.group(2)}"
    if _HTTP_RE.match(term):
        return term
    return None  # literal or unknown prefix


def _compact_iri(iri: str, prefixes: Mapping[str, str]) -> str:
    for prefix, base in prefixes.items():
        if iri.startswith(base):
            return f"{prefix}:{iri[len(base):]}"
    return iri


def _term_to_json_ld(term: str, prefixes: Mapping[str, str]) -> dict[str, Any]:
    iri = _resolve_iri(term, prefixes)
    if iri:
        return {"@id": iri}
    lang = re.match(r'^"(.*)"@([\w-]+)$', term, re.ASCII)
    if lang:
        return {"@value": lang.group(1), "@language": lang.group(2)}
    typed = re.match(r'^"(.*)"\^\^<?([\w:/#.-]+)>?$', term, re.ASCII)
    if typed:
        return {"@value": typed.group(1), "@type": _resolve_iri(typed.group(2), prefixes) or typed.group(2)}
    plain = re.match(r'^"(.*)"$', term)
    if plain:
        return {"@value": plain.group(1)}
    return {"@value": term}


def _json_ld_to_term(value: Any, prefixes: Mapping[str, str]) -> str | None:
    if not isinstance(value, dict):
        native = str(value)
        if _INT_RE.match(native) or _DECIMAL_RE.match(native) or native in ("true", "false"):
            return native
        return f'"{native}"'
    if value.get("@id"):
        return _compact_iri(str(value["@id"]), prefixes)
    if "@value" in value:
        if value.get("@language"):
            return f'"{value["@value"]}"@{value["@language"]}'
        if value.get("@type"):
            return f'"{value["@value"]}"^^{_compact_iri(str(value["@type"]), prefixes)}'
        return f'"{value["@value"]}"'
    return None


def quads_to_json_ld(quads: list[Quad], prefixes: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Quads -> expanded JSON-LD. Named graphs round-trip: quads carrying a
    ``graph`` become JSON-LD named-graph entries, graph-less quads land in the
    default graph. Provenance (source/rule) does not round-trip — JSON-LD
    carries the statements, the store keeps the evidence."""
    prefixes = dict(prefixes) if prefixes is not None else dict(JSONLD_PREFIXES)
    by_graph: dict[str, list[Quad]] = {}
    for q in quads:
        by_graph.setdefault(q.graph or "", []).append(q)

    def build_nodes(group: list[Quad]) -> list[dict[str, Any]]:
        nodes: dict[str, dict[str, Any]] = {}

        def node_of(id_: str) -> dict[str, Any]:
            if id_ not in nodes:
                nodes[id_] = {"@id": _resolve_iri(id_, prefixes) or id_}
            return nodes[id_]

        for q in group:
            node = node_of(q.subject)
            if q.predicate in ("rdf:type", "a"):
                type_iri = _resolve_iri(q.object, prefixes) or q.object
                existing = node.get("@type")
                if isinstance(existing, list):
                    if type_iri not in existing:
                        node["@type"] = [*existing, type_iri]
                elif existing is not None:
                    node["@type"] = [existing, type_iri]
                else:
                    node["@type"] = type_iri
                continue
            key = _resolve_iri(q.predicate, prefixes) or q.predicate
            value = _term_to_json_ld(q.object, prefixes)
            existing = node.get(key)
            node[key] = (
                [*existing, value]
                if isinstance(existing, list)
                else ([existing, value] if existing is not None else value)
            )
        return list(nodes.values())

    entries: list[Any] = []
    for g, group in by_graph.items():
        nodes = build_nodes(group)
        if g == "":
            entries.extend(nodes)
        else:
            entries.append({"@id": _resolve_iri(g, prefixes) or g, "@graph": nodes})
    return {"@context": prefixes, "@graph": entries}


def json_ld_to_quads(
    doc: Mapping[str, Any], prefixes: Mapping[str, str] | None = None
) -> list[Quad]:
    """Expanded JSON-LD -> quads (named graphs preserved; prefixed terms
    restored where the prefix map covers them)."""
    prefixes = prefixes if prefixes is not None else JSONLD_PREFIXES
    out: list[Quad] = []
    blank = 0

    def convert_node(node: Mapping[str, Any], graph_name: str | None) -> None:
        nonlocal blank
        if node.get("@id"):
            subject = _compact_iri(str(node["@id"]), prefixes)
        else:
            subject = f"_:b{blank}"
            blank += 1
        types = node.get("@type")
        for t in types if isinstance(types, list) else ([] if types is None else [types]):
            out.append(Quad(subject, "rdf:type", _compact_iri(str(t), prefixes), graph=graph_name))
        for key, raw in node.items():
            if key in ("@id", "@type"):
                continue
            resolved = _resolve_iri(key, prefixes) if ":" in key else key
            predicate = _compact_iri(resolved if resolved is not None else key, prefixes)
            for v in raw if isinstance(raw, list) else [raw]:
                obj = _json_ld_to_term(v, prefixes)
                if obj is not None:
                    out.append(Quad(subject, predicate, obj, graph=graph_name))

    for entry in doc.get("@graph", []):
        if isinstance(entry, dict) and isinstance(entry.get("@graph"), list):
            name = _compact_iri(str(entry["@id"]), prefixes) if entry.get("@id") else f"_:g{len(out)}"
            for node in entry["@graph"]:
                convert_node(node, name)
            continue
        if isinstance(entry, dict):
            convert_node(entry, None)
    return out


# ---------------------------------------------------------------------------
# Conformance battery
# ---------------------------------------------------------------------------


@dataclass
class ConformanceCheck:
    id: str
    #: SPARQL-subset SELECT. Passes when the row count is within
    #: [min_rows, max_rows]. Default: at least one row (ask-style);
    #: min_rows=0, max_rows=0 asserts absence.
    ask: str | None = None
    min_rows: int | None = None
    max_rows: int | None = None
    #: An extra SHACL shape the closure must satisfy.
    shape: NodeShape | None = None
    #: Why this check matters — surfaces in failure reports.
    reason: str | None = None


@dataclass
class ConformanceResult:
    id: str
    passed: bool
    detail: str


@dataclass
class ConformanceReport:
    passed: bool
    results: list[ConformanceResult] = field(default_factory=list)


async def run_conformance(store: InMemoryGraphStore, battery: list[ConformanceCheck]) -> ConformanceReport:
    """Run a battery of checks against a store's closure."""
    results: list[ConformanceResult] = []
    closure = await store.match()
    for check in battery:
        try:
            if check.ask:
                rows = await execute_select(store, check.ask)
                minimum = check.min_rows if check.min_rows is not None else 1
                ok = len(rows) >= minimum and (check.max_rows is None or len(rows) <= check.max_rows)
                detail = f"{len(rows)} rows (expected >= {minimum}"
                if check.max_rows is not None:
                    detail += f", <= {check.max_rows}"
                detail += ")"
                results.append(ConformanceResult(check.id, ok, detail))
            elif check.shape:
                # Battery shapes aren't store-registered: validate with a
                # scratch store carrying ONLY this shape (check_shapes is pure).
                scratch = InMemoryGraphStore(shapes=[check.shape])
                violations = scratch.check_shapes(closure).violations
                results.append(
                    ConformanceResult(
                        check.id,
                        not violations,
                        "; ".join(v.message for v in violations) if violations else "shape holds",
                    )
                )
            else:
                detail = "check has neither ask nor shape"
                if check.reason:
                    detail += f" ({check.reason})"
                results.append(ConformanceResult(check.id, False, detail))
        except Exception as err:  # noqa: BLE001 - battery reports, never throws
            results.append(ConformanceResult(check.id, False, str(err)))
    return ConformanceReport(passed=all(r.passed for r in results), results=results)


# ---------------------------------------------------------------------------
# TBox evolution
# ---------------------------------------------------------------------------


def _term_key(q: Any) -> str:
    return f"{q.subject}|{q.predicate}|{q.object}"


#: Ontology-defining predicates: shared with the review auditor.
TBOX_PREDICATES = {
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
}

#: rdf:type objects that mark a TERM declaration, not an instance fact.
OWL_VOCAB = {
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
}


@dataclass
class AxiomRef:
    """Exact axiom to retract (matched on subject|predicate|object)."""

    subject: str
    predicate: str
    object: str


@dataclass
class Rename:
    from_: str
    to: str


@dataclass
class Deprecation:
    term: str
    reason: str
    replacement: str | None = None


@dataclass
class TBoxMigrationSpec:
    from_: str
    to: str
    #: New TBox axioms to add (provenance required by the store).
    added: list[Quad] = field(default_factory=list)
    #: Exact axioms to retract (matched on subject|predicate|object).
    removed: list[AxiomRef] = field(default_factory=list)
    #: Term renames applied to BOTH the TBox and ABox in all three positions.
    renames: list[Rename] = field(default_factory=list)
    #: Terms deprecated in this version; with replacement, uses are rewritten.
    deprecations: list[Deprecation] = field(default_factory=list)
    #: Shapes/rules carried by the NEW store (defaults to the old store's).
    shapes: list[NodeShape] | None = None
    rules: list[GraphRule] | None = None


@dataclass
class MigrationOptions:
    battery: list[ConformanceCheck] | None = None
    factory: Any | None = None  # (opts: StoreOptions) -> InMemoryGraphStore
    #: Version-named graph for the new TBox. True/None -> default
    #: ``urn:framework:ontology:<to>``; False opts out (graph-free).
    tbox_graph: str | None | bool = None


@dataclass
class MigrationReport:
    from_: str
    to: str
    status: str = "migrated"  # migrated | aborted | failed
    added_quads: int = 0
    removed_quads: int = 0
    rewritten_quads: int = 0
    #: Named graph the new TBox axioms were written to (match(..., g=tboxGraph)).
    tbox_graph: str | None = None
    deprecated_warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    pre_battery: ConformanceReport | None = None
    post_battery: ConformanceReport | None = None


@dataclass
class MigrationResult:
    report: MigrationReport
    #: Present on success — the new-version store. The old store is untouched.
    store: InMemoryGraphStore | None = None


def _now_iso() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _is_axiom(q: Quad, predicates_in_use: set[str]) -> bool:
    """True when the quad is an ontology statement, not a fact."""
    if q.predicate in TBOX_PREDICATES:
        return True
    # rdf:type is TBox when it types a TERM (property/class used somewhere) or
    # carries an OWL vocabulary marker — ABox for plain instances.
    if q.predicate in RDF_TYPE:
        return q.subject in predicates_in_use or q.object in OWL_VOCAB
    return False


async def migrate_t_box(
    old_store: InMemoryGraphStore,
    spec: TBoxMigrationSpec,
    opts: MigrationOptions | None = None,
) -> MigrationResult:
    """Migrate a TBox. Order: pre-battery (abort on failure) -> rebuild ->
    post-battery (report failure; caller decides whether to adopt).

    The store has no delete by design; migration therefore REBUILDS: explicit
    facts are exported from the old store, transformed (axiom add/remove, term
    renames, deprecations), re-validated and re-entailed into a fresh store —
    the old store survives untouched (rollback is "don't adopt").
    """
    opts = opts or MigrationOptions()
    report = MigrationReport(from_=spec.from_, to=spec.to)
    if opts.battery:
        report.pre_battery = await run_conformance(old_store, opts.battery)
        if not report.pre_battery.passed:
            report.status = "aborted"
            report.errors.append("pre-migration conformance battery failed — ontology not migrated")
            return MigrationResult(report=report)

    remove_keys = {_term_key(q) for q in (spec.removed or [])}
    rename_map = {r.from_: r.to for r in (spec.renames or [])}
    for d in spec.deprecations or []:
        if d.replacement:
            rename_map[d.term] = d.replacement
            report.deprecated_warnings.append(f"{d.term} deprecated ({d.reason}) — rewritten to {d.replacement}")
        else:
            report.deprecated_warnings.append(f"{d.term} deprecated ({d.reason}) — uses kept, flagged")

    def rewrite(term: str) -> str:
        return rename_map.get(term, term)

    factory = opts.factory or (lambda o: InMemoryGraphStore(o))
    new_store = factory(
        StoreOptions(
            shapes=spec.shapes if spec.shapes is not None else old_store.ontology_shapes(),
            rules=spec.rules if spec.rules is not None else old_store.ontology_rules(),
        )
    )

    explicit = [q for q in await old_store.match() if q.source != "entailment"]
    # Named-graph use: the new ontology is its OWN dataset inside the store —
    # every version stays addressable (match(None, None, None, tbox_graph)),
    # JSON-LD round-trips it as a named graph, and False opts out.
    if opts.tbox_graph is False:
        tbox_graph: str | None = None
    else:
        tbox_graph = opts.tbox_graph or f"urn:framework:ontology:{spec.to}"
    report.tbox_graph = tbox_graph
    predicates_in_use = {
        rewrite(q.predicate) for q in explicit if _term_key(q) not in remove_keys
    }

    migration_tag = f"migration:{spec.from_}->{spec.to}"
    rebuilt: list[Quad] = []
    for q in explicit:
        if _term_key(q) in remove_keys:
            report.removed_quads += 1
            continue
        next_q = Quad(
            rewrite(q.subject),
            rewrite(q.predicate),
            rewrite(q.object),
            graph=q.graph,
            source=q.source,
            derived_from=q.derived_from,
        )
        if _is_axiom(next_q, predicates_in_use) and tbox_graph:
            next_q.graph = tbox_graph
        if _term_key(next_q) != _term_key(q):
            report.rewritten_quads += 1
            next_q.derived_from = (
                f"{next_q.derived_from};{migration_tag}" if next_q.derived_from else migration_tag
            )
        rebuilt.append(next_q)
    for aq in spec.added or []:
        rebuilt.append(
            Quad(
                aq.subject,
                aq.predicate,
                aq.object,
                graph=tbox_graph if tbox_graph else aq.graph,
                source=aq.source,
                derived_from=migration_tag,
            )
        )
    report.added_quads = len(spec.added or [])

    try:
        await new_store.add(rebuilt)
    except Exception as err:  # noqa: BLE001 - migration reports, never throws
        report.status = "failed"
        report.errors.append(f"{spec.to} store rejected migrated facts: {err}")
        return MigrationResult(report=report)

    if opts.battery:
        report.post_battery = await run_conformance(new_store, opts.battery)
        if not report.post_battery.passed:
            report.status = "failed"
            failed = [f"{r.id}: {r.detail}" for r in report.post_battery.results if not r.passed]
            report.errors.append(f"post-migration conformance battery failed — {'; '.join(failed)}")
            return MigrationResult(report=report)

    return MigrationResult(report=report, store=new_store)