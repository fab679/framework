"""Ontology self-review + SHACL induction. Parity:
packages/typescript/src/semantic/ontology-review.ts.

The agent audits what its own ontology IS — not what it wishes it were —
directly from the store, and identifies what to add:

1. review_ontology(): inventory + gaps. Used-but-undeclared terms,
   declared-but-unused terms, unlabeled terms, properties missing
   rdfs:domain/range. "Where is X used?", "what's missing?" — one call.
2. propose_shapes(): SHACL INDUCTION from instance data — observe each
   class's instances, emit candidate NodeShapes (min/maxCount, datatype,
   nodeKind) that explain the observed data. The builder owns shapes: these
   are PROPOSALS — adopt via adopt_shapes() (validates the existing closure
   first) or the next migrate_t_box(shapes=...); never a silent adoption.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from .semantic import (
    InMemoryGraphStore,
    LABEL_PREDICATES,
    OWL_VOCAB,
    RDF_TYPE,
    TBOX_PREDICATES,
    NodeShape,
    PropertyShape,
    Quad,
    is_literal,
    strip_literal,
    term_datatype,
)
from .tools import Tool, ToolContext, define_tool


@dataclass
class TermStat:
    term: str
    kind: str
    #: Explicit ABox uses: instance-of counts for classes, fact counts for
    #: properties.
    uses: int = 0
    #: TBox axioms mentioning the term (declarations, subClassOf, domain/range...).
    axiom_count: int = 0
    label: str | None = None


@dataclass
class OntologyReview:
    classes: list[TermStat] = field(default_factory=list)
    properties: list[TermStat] = field(default_factory=list)
    #: Used in data with zero TBox declarations — the "what to add" list.
    undeclared: list[TermStat] = field(default_factory=list)
    #: Declared but never used in any explicit fact.
    unused: list[str] = field(default_factory=list)
    #: Terms without rdfs:label / skos:prefLabel / altLabel.
    unlabeled: list[str] = field(default_factory=list)
    #: Properties missing rdfs:domain / rdfs:range axioms.
    missing_domain: list[str] = field(default_factory=list)
    missing_range: list[str] = field(default_factory=list)
    #: Explicit quads vs closure size — how much the ontology explains.
    explicit_quads: int = 0
    closure_quads: int = 0


def _is_axiom_quad(q: Quad, predicates_in_use: set[str]) -> bool:
    """True when the quad is an ontology statement, not a fact."""
    return q.predicate in TBOX_PREDICATES or (
        q.predicate in RDF_TYPE and (q.subject in predicates_in_use or q.object in OWL_VOCAB)
    )


async def review_ontology(store: InMemoryGraphStore) -> OntologyReview:
    """Audit the store: inventory + gap list. Pure read — never mutates."""
    closure = await store.match()
    explicit = [q for q in closure if q.source != "entailment"]
    predicates_in_use = {q.predicate for q in explicit}

    labels: dict[str, str] = {}
    for q in closure:
        if q.predicate in LABEL_PREDICATES:
            labels[q.subject] = strip_literal(q.object)

    classes: dict[str, TermStat] = {}
    properties: dict[str, TermStat] = {}

    def class_(term: str) -> TermStat:
        if term not in classes:
            classes[term] = TermStat(term=term, kind="class", label=labels.get(term))
        return classes[term]

    def property_(term: str) -> TermStat:
        if term not in properties:
            properties[term] = TermStat(term=term, kind="property", label=labels.get(term))
        return properties[term]

    domains: dict[str, int] = {}
    ranges: dict[str, int] = {}
    for q in explicit:
        axiom = _is_axiom_quad(q, predicates_in_use)

        # rdf:type: axiom = term declaration (TBox); fact = instance typing (ABox).
        if q.predicate in RDF_TYPE:
            if axiom:
                class_(q.subject).axiom_count += 1
            else:
                class_(q.object).uses += 1
            continue

        if q.predicate in TBOX_PREDICATES:
            # TBox statements introduce/declare their subject term.
            subject = class_(q.subject) if "subClassOf" in q.predicate else property_(q.subject)
            subject.axiom_count += 1
            # Their object is a class term (domain/range/subClassOf targets).
            if "inverseOf" not in q.predicate:
                class_(q.object).axiom_count += 1
            if "domain" in q.predicate:
                domains[q.subject] = domains.get(q.subject, 0) + 1
            if "range" in q.predicate:
                ranges[q.subject] = ranges.get(q.subject, 0) + 1
            continue

        # ABox fact: a property use with actual data behind it.
        property_(q.predicate).uses += 1

    all_terms = [*classes.values(), *properties.values()]
    undeclared = sorted(
        (t for t in all_terms if t.uses > 0 and t.axiom_count == 0),
        key=lambda t: -t.uses,
    )
    unused = [t.term for t in all_terms if t.axiom_count > 0 and t.uses == 0]
    unlabeled = [t.term for t in all_terms if not t.label]
    known_props = [t for t in properties.values() if t.axiom_count > 0 or t.uses > 0]
    missing_domain = [t.term for t in known_props if t.term not in domains]
    missing_range = [t.term for t in known_props if t.term not in ranges]

    return OntologyReview(
        classes=sorted(classes.values(), key=lambda t: (-t.uses, t.term)),
        properties=sorted(properties.values(), key=lambda t: (-t.uses, t.term)),
        undeclared=undeclared,
        unused=unused,
        unlabeled=unlabeled,
        missing_domain=missing_domain,
        missing_range=missing_range,
        explicit_quads=len(explicit),
        closure_quads=len(closure),
    )


# ---------------------------------------------------------------------------
# SHACL induction from instance data
# ---------------------------------------------------------------------------


async def propose_shapes(
    store: InMemoryGraphStore,
    min_instances: int = 2,
    coverage: float = 0.5,
) -> list[NodeShape]:
    """Propose NodeShapes by observing explicit instance data: per class,
    per-property coverage, cardinality, datatypes, node kinds across its
    instances. PROPOSAL ONLY — shapes are builder-owned."""
    explicit = [q for q in await store.match() if q.source != "entailment"]
    predicates_in_use = {q.predicate for q in explicit}

    # Class -> instance subjects, from EXPLICIT type facts only (closure types
    # would smear subclass properties up into parent shapes).
    instances_of: dict[str, set[str]] = {}
    for q in explicit:
        if q.predicate not in RDF_TYPE or _is_axiom_quad(q, predicates_in_use):
            continue
        instances_of.setdefault(q.object, set()).add(q.subject)

    shapes: list[NodeShape] = []
    for cls, subjects in instances_of.items():
        if len(subjects) < min_instances:
            continue
        by_subject: dict[str, list[Quad]] = {}
        for q in explicit:
            if q.subject in subjects:
                by_subject.setdefault(q.subject, []).append(q)
        observed: dict[str, dict[str, Any]] = {}
        for quads in by_subject.values():
            by_path: dict[str, list[Quad]] = {}
            for q in quads:
                if q.predicate in RDF_TYPE:
                    continue
                by_path.setdefault(q.predicate, []).append(q)
            for path, path_quads in by_path.items():
                o = observed.setdefault(
                    path, {"present": 0, "max_seen": 0, "datatypes": set(), "node_kinds": set()}
                )
                o["present"] += 1
                o["max_seen"] = max(o["max_seen"], len(path_quads))
                for q in path_quads:
                    o["node_kinds"].add("literal" if is_literal(q.object) else "iri")
                    dt = term_datatype(q.object)
                    if dt:
                        o["datatypes"].add(dt)
        properties: list[PropertyShape] = []
        for path, o in observed.items():
            if o["present"] / len(subjects) < coverage:
                continue
            shape = PropertyShape(path=path)
            if o["present"] == len(subjects):
                shape.min_count = 1
            if o["max_seen"] == 1:
                shape.max_count = 1
            if len(o["datatypes"]) == 1:
                shape.datatype = next(iter(o["datatypes"]))
            if len(o["node_kinds"]) == 1:
                shape.node_kind = next(iter(o["node_kinds"]))
            properties.append(shape)
        if properties:
            shapes.append(NodeShape(target_class=cls, properties=properties))
    return shapes


@dataclass
class RejectedShape:
    shape: NodeShape
    violations: list[str]


@dataclass
class ShapeAdoption:
    adopted: list[NodeShape] = field(default_factory=list)
    rejected: list[RejectedShape] = field(default_factory=list)


async def adopt_shapes(store: InMemoryGraphStore, shapes: list[NodeShape]) -> ShapeAdoption:
    """Adopt proposed shapes: each shape is validated against the CURRENT
    closure in isolation — a shape the existing data already violates is
    rejected with its violations (1:1 mapping, no guesswork), the clean ones
    are registered. Versioned adoption: migrate_t_box(shapes=...) instead."""
    closure = await store.match()
    adoption = ShapeAdoption()
    for shape in shapes:
        # Validate with ONLY this shape: violations map to it unambiguously.
        violations = InMemoryGraphStore(shapes=[shape]).check_shapes(closure).violations
        if violations:
            adoption.rejected.append(RejectedShape(shape=shape, violations=[v.message for v in violations]))
        else:
            adoption.adopted.append(shape)
    if adoption.adopted:
        store.add_shapes(*adoption.adopted)
    return adoption


# ---------------------------------------------------------------------------
# Agent tools
# ---------------------------------------------------------------------------


class _NoArgs(BaseModel):
    pass


def review_ontology_tool(store: InMemoryGraphStore) -> Tool:
    """review_ontology agent tool: inventory + gaps, straight from the store."""

    async def execute(args: _NoArgs, ctx: ToolContext | None) -> dict[str, Any]:
        review = await review_ontology(store)

        def stat(t: TermStat) -> dict[str, Any]:
            return {
                "term": t.term,
                "kind": t.kind,
                "uses": t.uses,
                "axiomCount": t.axiom_count,
                **({"label": t.label} if t.label is not None else {}),
            }

        return {
            "classes": [stat(t) for t in review.classes],
            "properties": [stat(t) for t in review.properties],
            "undeclared": [stat(t) for t in review.undeclared],
            "unused": review.unused,
            "unlabeled": review.unlabeled,
            "missingDomain": review.missing_domain,
            "missingRange": review.missing_range,
            "explicitQuads": review.explicit_quads,
            "closureQuads": review.closure_quads,
        }

    return define_tool(
        name="review_ontology",
        description="Audit the knowledge graph's ontology: classes/properties with usage counts, used-but-undeclared terms, unlabeled/unused terms, properties missing domain or range. Read-only.",
        schema=_NoArgs,
        execute=execute,
    )


def propose_shapes_tool(
    store: InMemoryGraphStore,
    min_instances: int = 2,
    coverage: float = 0.5,
) -> Tool:
    """propose_shapes agent tool: SHACL candidates induced from instance data."""

    async def shape_json(s: NodeShape) -> dict[str, Any]:
        return {
            "targetClass": s.target_class,
            "properties": [
                {k: v for k, v in p.__dict__.items() if v is not None} for p in s.properties
            ],
        }

    async def execute(args: _NoArgs, ctx: ToolContext | None) -> list[dict[str, Any]]:
        return [await shape_json(s) for s in await propose_shapes(store, min_instances, coverage)]

    return define_tool(
        name="propose_shapes",
        description="Induce candidate SHACL shapes from existing instance data (>= 2 instances per class, >= 50% property coverage). PROPOSAL: adoption is the builder's call via adopt_shapes(), never automatic.",
        schema=_NoArgs,
        execute=execute,
    )