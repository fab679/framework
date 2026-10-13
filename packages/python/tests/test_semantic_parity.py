"""Semantic layer parity tests. Mirror: packages/typescript/tests/unit/semantic3.test.ts
(+ the P1-sem store semantics it builds on).

Naming note (deliberate TS->Python difference): the migration TBox named
graph is ``urn:framework:ontology:<version>`` — the TS suite uses a
personal-name prefix; the tests here pin the swap (the TS default embeds one).
"""

from __future__ import annotations

import time

import httpx
import pytest

from framework_agents.errors import AgentError
from framework_agents.memory import InMemoryMemory
from framework_agents.ontology_review import (
    adopt_shapes,
    propose_shapes,
    propose_shapes_tool,
    review_ontology,
    review_ontology_tool,
)
from framework_agents.remote_sparql import (
    RemoteSparqlAdapter,
    SparqlAuth,
    remote_sparql_tool,
)
from framework_agents.semantic import (
    AxiomRef,
    ConformanceCheck,
    Deprecation,
    EntityLinker,
    GraphMemory,
    GraphRule,
    InMemoryGraphStore,
    JSONLD_PREFIXES,
    MappingProposals,
    MigrationOptions,
    NodeShape,
    PropertyShape,
    Quad,
    Rename,
    TBoxMigrationSpec,
    TriplePattern,
    claim_check_tool,
    execute_select,
    graph_add_tool,
    json_ld_to_quads,
    link_entity_tool,
    migrate_t_box,
    ontology_lookup_tool,
    propose_mapping_tool,
    quads_to_json_ld,
    run_conformance,
    sparql_select_tool,
)
from framework_agents.tools import ToolRegistry, parameters_schema
from framework_agents.types import assistant as msg_assistant
from framework_agents.types import user as msg_user


def quad(s: str, p: str, o: str) -> Quad:
    return Quad(s, p, o, source="test")


# ---------------------------------------------------------------------------
# Entailment closure + why() proofs
# ---------------------------------------------------------------------------


async def test_owl_axioms_entail_with_proof_rules():
    store = InMemoryGraphStore()
    await store.add(
        [
            quad("ex:partOf", "rdf:type", "owl:TransitiveProperty"),
            quad("ex:knows", "rdf:type", "owl:SymmetricProperty"),
            quad("ex:worksAt", "owl:inverseOf", "ex:employedBy"),
            quad("ex:a", "ex:partOf", "ex:b"),
            quad("ex:b", "ex:partOf", "ex:c"),
            quad("ex:ada", "ex:knows", "ex:bo"),
            quad("ex:acme", "ex:employedBy", "ex:ada"),
        ]
    )
    # Transitive: a partOf c is entailed, with premises and a rule id.
    transitive = await store.match("ex:a", "ex:partOf", "ex:c")
    assert len(transitive) == 1
    assert transitive[0].source == "entailment"
    why = await store.why(transitive[0])
    assert why is not None
    assert why.rule == "OWL-TRANSITIVE"
    assert {(p.subject, p.object) for p in why.premises} == {("ex:a", "ex:b"), ("ex:b", "ex:c")}
    # Symmetric: bo knows ada.
    symmetric = await store.match("ex:bo", "ex:knows", "ex:ada")
    assert len(symmetric) == 1
    assert (await store.why(symmetric[0])).rule == "OWL-SYMMETRIC"
    # InverseOf both directions: ada worksAt acme derives from the employedBy fact.
    inverse = await store.match("ex:ada", "ex:worksAt", "ex:acme")
    assert len(inverse) == 1
    assert (await store.why(inverse[0])).rule == "OWL-INVERSE"
    # Explicit facts carry no derivation (why() is None).
    assert await store.why(quad("ex:a", "ex:partOf", "ex:b")) is None


async def test_rdfs_subset_fixpoint():
    store = InMemoryGraphStore()
    await store.add(
        [
            quad("ex:Manager", "rdfs:subClassOf", "ex:Employee"),
            quad("ex:Employee", "rdfs:subClassOf", "ex:Person"),
            quad("ex:ada", "rdf:type", "ex:Manager"),
            quad("ex:worksFor", "rdfs:domain", "ex:Person"),
            quad("ex:worksFor", "rdfs:range", "ex:Company"),
            quad("ex:ada", "ex:worksFor", "ex:acme"),
        ]
    )
    # SUB-TRANS: Manager subClassOf Person (chain transitively closed).
    chain = await store.match("ex:Manager", "rdfs:subClassOf", "ex:Person")
    assert len(chain) == 1 and (await store.why(chain[0])).rule == "RDFS-SUB-TRANS"
    # TYPE via subClassOf: ada is an Employee (and so a Person) through the chain.
    employee_type = await store.match("ex:ada", "rdf:type", "ex:Employee")
    assert len(employee_type) == 1 and (await store.why(employee_type[0])).rule == "RDFS-TYPE"
    person_type = await store.match("ex:ada", "rdf:type", "ex:Person")
    assert len(person_type) == 1
    # The Person-typing key is claimed by whichever rule inserts first in the
    # fixpoint pass — here DOMAIN (same derivation-order behavior as TS).
    assert (await store.why(person_type[0])).rule == "RDFS-DOMAIN"
    # RANGE: acme (object of ex:worksFor) is typed ex:Company.
    ranged = await store.match("ex:acme", "rdf:type", "ex:Company")
    assert len(ranged) == 1 and (await store.why(ranged[0])).rule == "RDFS-RANGE"
    # DOMAIN: ada (subject of ex:worksFor) is typed ex:Person anyway; acme gets ex:Company.
    ranged = await store.match("ex:acme", "rdf:type", "ex:Company")
    assert len(ranged) == 1 and (await store.why(ranged[0])).rule == "RDFS-RANGE"
    domained = await store.match("ex:ada", "rdf:type", "ex:Person")
    assert len(domained) == 1


async def test_functional_conflict_is_an_error_with_rollback():
    store = InMemoryGraphStore()
    await store.add(
        [
            quad("ex:birthYear", "rdf:type", "owl:FunctionalProperty"),
            quad("ex:ada", "ex:birthYear", '"1815"'),
        ]
    )
    size_before = len(await store.match())
    with pytest.raises(ValueError, match="Functional property violation"):
        await store.add([quad("ex:ada", "ex:birthYear", '"1816"')])
    # Rolled back: exactly one birthYear, no second fact, no residue.
    assert len(await store.match("ex:ada", "ex:birthYear", None)) == 1
    assert await store.match("ex:ada", "ex:birthYear", '"1816"') == []
    assert len(await store.match()) == size_before


async def test_shacl_subset_enforcement_with_rollback():
    shape = NodeShape(target_class="ex:Person", properties=[PropertyShape(path="ex:name", min_count=1)])
    store = InMemoryGraphStore(shapes=[shape])
    await store.add([quad("ex:ada", "rdf:type", "ex:Person"), quad("ex:ada", "ex:name", '"Ada"')])
    with pytest.raises(ValueError, match="SHACL violation"):
        await store.add([quad("ex:bo", "rdf:type", "ex:Person")])
    # Rollback: bo never entered the store.
    assert await store.match("ex:bo", "rdf:type", None) == []
    assert len(await store.match()) == 2


async def test_shacl_violations_are_recorded():
    scratch = InMemoryGraphStore(
        shapes=[
            NodeShape(
                target_class="ex:Person",
                properties=[
                    PropertyShape(path="ex:name", min_count=1, max_count=1, datatype="xsd:string"),
                    PropertyShape(path="ex:worksFor", node_kind="iri"),
                    PropertyShape(path="ex:age", datatype="xsd:integer"),
                    PropertyShape(path="ex:nickname", min_count=1),
                ],
            )
        ]
    )
    report = scratch.check_shapes(
        [
            quad("ex:x", "rdf:type", "ex:Person"),
            quad("ex:x", "ex:name", '"Ada"'),
            quad("ex:x", "ex:name", '"Bo"'),  # maxCount 1 violated
            quad("ex:x", "ex:worksFor", '"acme"'),  # nodeKind iri violated
            quad("ex:x", "ex:age", '"nine"'),  # datatype violated
        ]
    )
    assert not report.valid
    messages = [v.message for v in report.violations]
    assert any("needs >= 1 ex:nickname" in m for m in messages)  # minCount
    assert any("allows <= 1 ex:name" in m for m in messages)  # maxCount
    assert any("must be an IRI" in m for m in messages)  # nodeKind iri
    assert any("is not xsd:integer" in m for m in messages)  # datatype
    # minCount violations carry node + path for repair.
    assert any(v.node == "ex:x" and v.path == "ex:nickname" for v in report.violations)


async def test_versioned_rules_derive_with_rule_tags():
    rule = GraphRule(
        id="rule-in-country",
        version="4",
        where=[
            TriplePattern("?c", "ex:locatedIn", "?country"),
            TriplePattern("?country", "rdf:type", "ex:Country"),
        ],
        construct=[TriplePattern("?c", "ex:inCountry", "?country")],
    )
    store = InMemoryGraphStore(rules=[rule])
    await store.add([quad("ex:paris", "ex:locatedIn", "ex:france"), quad("ex:france", "rdf:type", "ex:Country")])
    derived = await store.match("ex:paris", "ex:inCountry", "ex:france")
    assert len(derived) == 1
    assert derived[0].rule == "rule-in-country@4"
    assert derived[0].derived_from == "rule-in-country@4"
    why = await store.why(derived[0])
    assert why is not None
    assert why.rule == "rule-in-country@4"
    assert len(why.premises) == 2


# ---------------------------------------------------------------------------
# SELECT subset + named graph identity
# ---------------------------------------------------------------------------


async def test_select_conjunctive_join():
    store = InMemoryGraphStore()
    await store.add(
        [
            quad("ex:ada", "ex:name", '"Ada"'),
            quad("ex:ada", "ex:worksFor", "ex:acme"),
            quad("ex:bo", "ex:name", '"Bo"'),
            quad("ex:bo", "ex:worksFor", "ex:biomed"),
        ]
    )
    rows = await execute_select(store, "SELECT ?name WHERE { ?s ex:name ?name . ?s ex:worksFor ex:acme . }")
    assert rows == [{"?name": '"Ada"'}]
    rows_all = await execute_select(store, "SELECT ?s ?name WHERE { ?s ex:name ?name . }")
    assert sorted(r["?s"] for r in rows_all) == ["ex:ada", "ex:bo"]


async def test_named_graph_identity_is_part_of_the_key():
    store = InMemoryGraphStore()
    await store.add(
        [
            Quad("ex:ada", "ex:worksFor", "ex:acme", graph="urn:test:hr", source="test"),
            Quad("ex:ada", "ex:worksFor", "ex:acme", graph="urn:test:legacy", source="test"),
        ]
    )
    # PIN: same s/p/o in two graphs = TWO quads.
    assert len(await store.match("ex:ada", "ex:worksFor", "ex:acme")) == 2
    # match() with a graph value selects ONE named graph.
    assert len(await store.match("ex:ada", "ex:worksFor", "ex:acme", "urn:test:hr")) == 1
    assert len(await store.match("ex:ada", "ex:worksFor", "ex:acme", "urn:test:legacy")) == 1
    # Dedup: re-adding an identical quad (graph included) changes nothing.
    await store.add([Quad("ex:ada", "ex:worksFor", "ex:acme", graph="urn:test:hr", source="test")])
    assert len(await store.match("ex:ada", "ex:worksFor", "ex:acme")) == 2


# ---------------------------------------------------------------------------
# Agent tools: validated writes + claim checks
# ---------------------------------------------------------------------------


async def test_graph_add_returns_repair_feedback_not_raises():
    store = InMemoryGraphStore(shapes=[NodeShape(target_class="ex:Person", properties=[PropertyShape(path="ex:name", min_count=1)])])
    reg = ToolRegistry().register(graph_add_tool(store))
    # SHACL violation — feedback string, nothing committed.
    out = await reg.call("graph_add", {"quads": [{"subject": "ex:bo", "predicate": "rdf:type", "object": "ex:Person", "source": "test"}]})
    assert isinstance(out, str)
    assert out.startswith("Validation failed — fix and retry:")
    assert "SHACL violation" in out
    assert len(await store.match()) == 0
    # Valid write commits (and counts the closure's inferred quads).
    out2 = await reg.call(
        "graph_add",
        {"quads": [{"subject": "ex:bo", "predicate": "rdf:type", "object": "ex:Person", "source": "test"},
                   {"subject": "ex:bo", "predicate": "ex:name", "object": '"Bo"', "source": "test"}]},
    )
    assert out2 == {"committed": 2, "inferred": 0}
    assert len(await store.match("ex:bo")) == 2


async def test_claim_check_grounded_unknown_contradicted():
    store = InMemoryGraphStore()
    await store.add(
        [
            quad("ex:birthYear", "rdf:type", "owl:FunctionalProperty"),
            quad("ex:ada", "ex:birthYear", '"1815"'),
            quad("ex:ada", "ex:knows", "ex:bo"),
        ]
    )
    reg = ToolRegistry().register(claim_check_tool(store))
    grounded = await reg.call("claim_check", {"subject": "ex:ada", "predicate": "ex:birthYear", "object": '"1815"'})
    assert grounded["verdict"] == "grounded"
    assert grounded["proof"] is None  # explicit fact — no derivation
    contradicted = await reg.call("claim_check", {"subject": "ex:ada", "predicate": "ex:birthYear", "object": '"1816"'})
    assert contradicted["verdict"] == "contradicted"
    assert contradicted["proof"] == {"existing": ['"1815"']}
    unknown = await reg.call("claim_check", {"subject": "ex:ada", "predicate": "ex:height", "object": '"170"'})
    assert unknown == {"verdict": "unknown", "proof": None}
    # Non-functional: a different value for the same predicate is "unknown"
    # (the graph holds related values, not a contradiction).
    related = await reg.call("claim_check", {"subject": "ex:ada", "predicate": "ex:knows", "object": "ex:cyd"})
    assert related["verdict"] == "unknown"
    assert related["proof"] == {"related": ["ex:bo"]}


async def test_read_tools_and_schemas_are_agent_compatible():
    store = InMemoryGraphStore()
    await store.add([quad("ex:ada", "rdfs:label", '"Ada Lovelace"'), quad("ex:bo", "skos:prefLabel", '"Bo"')])
    tools = [
        sparql_select_tool(store),
        ontology_lookup_tool(store),
        graph_add_tool(store),
        claim_check_tool(store),
        link_entity_tool(EntityLinker(store)),
        propose_mapping_tool(MappingProposals()),
    ]
    for t in tools:
        # JSON Schema present -> usable as plain Agent options.tools entries.
        assert parameters_schema(t) is not None
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    rows = await reg.call("sparql_select", {"query": "SELECT ?s WHERE { ?s skos:prefLabel ?l . }"})
    assert rows == [{"?s": "ex:bo"}]
    hits = await reg.call("ontology_lookup", {"term": "ada"})
    assert hits == [{"subject": "ex:ada", "predicate": "rdfs:label", "object": '"Ada Lovelace"'}]


# ---------------------------------------------------------------------------
# Entity linking + sameAs + mapping autonomy
# ---------------------------------------------------------------------------


async def linked_store() -> InMemoryGraphStore:
    store = InMemoryGraphStore()
    await store.add(
        [
            quad("ex:ada", "rdf:type", "ex:Person"),
            quad("ex:ada", "rdfs:label", '"Ada Lovelace"'),
            Quad("wd:Q7251", "owl:sameAs", "ex:ada", source="test"),
            quad("wd:Q7251", "rdfs:label", '"Ada"'),
            quad("ex:bo", "skos:prefLabel", '"Bo"'),
        ]
    )
    return store


async def test_entity_linker_canonicalizes_through_sameas():
    store = await linked_store()
    hits = await EntityLinker(store).link("Ada")
    assert len(hits) >= 1
    assert hits[0].iri in ("ex:ada", "wd:Q7251")
    # wd:Q7251 and ex:ada are ONE entity — one canonical answer with the group.
    assert len(hits[0].same_as_group) == 2
    assert set(hits[0].same_as_group) == {"ex:ada", "wd:Q7251"}
    assert hits[0].score >= 100
    # Local-name matches score below exact labels but still surface.
    reg = ToolRegistry().register(link_entity_tool(EntityLinker(store)))
    bo = await reg.call("link_entity", {"mention": "Bo"})
    assert bo[0]["iri"] == "ex:bo"
    assert bo[0]["score"] == 100
    assert bo[0]["same_as_group"] == ["ex:bo"]


async def test_mapping_proposals_default_autonomy_commits():
    store = await linked_store()
    auto = MappingProposals(store=store)
    assert auto.is_autonomous is True
    committed = await auto.propose("engineer_name", "ex:ada", "hr:import")
    assert committed.status == "approved"
    assert committed.committed_by_autonomy is True
    # The alias is visible to the linker immediately.
    hits = await EntityLinker(store).link("engineer_name")
    assert "ex:ada" in hits[0].same_as_group
    again = await auto.propose("engineer_name", "ex:ada", "hr:import")
    assert again.id == committed.id  # re-proposal dedupes, no second write
    counts = len(await store.match("ex:ada", "skos:altLabel", None))
    assert counts == 1
    # Explicit opt-out queues instead — decide() is the human turn.
    review = MappingProposals(autonomy=False)
    assert review.is_autonomous is False
    pending = await review.propose("account_id", "ex:acme", "crm:import")
    assert pending.status == "pending"
    approved = await review.decide(store, pending.id, "approved")
    assert approved.status == "approved"
    linked = await EntityLinker(store).link("account_id")
    assert linked and linked[0].iri == "ex:acme"
    # Rejected proposals never write.
    p2 = await review.propose("x", "ex:bo", "s")
    rejected = await review.decide(store, p2.id, "rejected")
    assert rejected.status == "rejected"
    assert await store.match("ex:bo", "skos:altLabel", '"x"') == []
    # No store bound: pending even with autonomy on.
    orphan = MappingProposals()
    assert orphan.is_autonomous is False
    assert (await orphan.propose("y", "ex:bo", "s")).status == "pending"


async def test_propose_mapping_tool_feedback_on_validation_error():
    store = InMemoryGraphStore()
    await store.add([quad("skos:altLabel", "rdf:type", "owl:FunctionalProperty")])
    tool = propose_mapping_tool(MappingProposals(store=store))
    reg = ToolRegistry().register(tool)
    first = await reg.call("propose_mapping", {"from": "one", "to": "ex:ada", "source": "s"})
    assert first == {"id": "map-1", "status": "approved", "committed": True}
    # Second DIFFERENT alias on the same target clashes with the functional
    # property: feedback string, not a raised tool error.
    second = await reg.call("propose_mapping", {"from": "two", "to": "ex:ada", "source": "s"})
    assert isinstance(second, str)
    assert second.startswith("Commit rejected — fix and retry:")
    assert "Functional property violation" in second
    assert len(await store.match("ex:ada", "skos:altLabel", None)) == 1


# ---------------------------------------------------------------------------
# JSON-LD I/O
# ---------------------------------------------------------------------------


async def test_jsonld_round_trip_expanded_form():
    quads = [
        quad("ex:ada", "rdf:type", "ex:Person"),
        quad("ex:ada", "rdfs:label", '"Ada"@en'),
        quad("ex:ada", "ex:birthDate", '"1815-12-10"^^xsd:date'),
        quad("ex:ada", "ex:worksFor", "ex:acme"),
        quad("https://data.example.org/thing", "rdfs:label", '"Full IRI subject"'),
    ]
    doc = quads_to_json_ld(quads)
    ada = next(n for n in doc["@graph"] if str(n.get("@id", "")).endswith("ada"))
    assert ada["@id"] == "urn:example:ada"
    assert ada["@type"] == "urn:example:Person"
    assert ada["http://www.w3.org/2000/01/rdf-schema#label"] == {"@value": "Ada", "@language": "en"}
    assert ada["urn:example:birthDate"] == {
        "@value": "1815-12-10",
        "@type": "http://www.w3.org/2001/XMLSchema#date",
    }
    assert ada["urn:example:worksFor"] == {"@id": "urn:example:acme"}
    back = json_ld_to_quads({"@graph": doc["@graph"]})
    sorted_keys = sorted(f"{q.subject}|{q.predicate}|{q.object}" for q in back)
    assert sorted_keys == sorted(f"{q.subject}|{q.predicate}|{q.object}" for q in quads)


async def test_jsonld_named_graph_round_trip():
    quads = [
        Quad("ex:Employee", "rdfs:subClassOf", "ex:Person", graph="urn:framework:ontology:v4", source="test"),
        quad("ex:ada", "rdf:type", "ex:Employee"),  # default graph
    ]
    doc = quads_to_json_ld(quads)
    named = next(n for n in doc["@graph"] if isinstance(n.get("@graph"), list))
    assert named["@id"] == "urn:framework:ontology:v4"
    back = json_ld_to_quads({"@graph": doc["@graph"]})
    assert len(back) == 2
    sub_class_of = next(q for q in back if q.predicate == "rdfs:subClassOf")
    assert sub_class_of.graph == "urn:framework:ontology:v4"
    typed = next(q for q in back if q.predicate == "rdf:type")
    assert typed.graph is None
    # Prefix map is configurable; default includes the TS DEFAULTS + ex:.
    assert JSONLD_PREFIXES["ex"] == "urn:example:"


# ---------------------------------------------------------------------------
# GraphMemory
# ---------------------------------------------------------------------------


async def test_graph_memory_injects_topk_facts_for_last_user_turn():
    store = InMemoryGraphStore()
    await store.add(
        [
            quad("ex:ada", "ex:worksFor", "ex:acme"),
            quad("ex:bo", "ex:name", '"Bo"'),
            quad("ex:ada", "ex:name", '"Ada"'),
        ]
    )
    inner = InMemoryMemory()
    await inner.save([msg_user("Where does ada work?"), msg_assistant("Let me check the graph.")])
    mem = GraphMemory(store, inner)
    messages = await mem.load()
    assert len(messages) == 3  # inner untouched + one knowledge message
    injected = messages[-1]
    assert injected.role == "assistant"
    assert injected.content.startswith("Relevant known facts (ground answers in these):")
    assert "- ex:ada ex:worksFor ex:acme" in injected.content
    assert '"Bo"' not in injected.content  # irrelevant facts not injected
    # save/clear delegate to the inner memory (2 kept + 1 appended).
    await mem.save([msg_assistant("done")])
    assert len(await inner.load()) == 3
    await mem.clear()
    assert await inner.load() == []
    # No user turn -> no injection.
    inner2 = InMemoryMemory()
    await inner2.save([msg_assistant("only me")])
    assert await GraphMemory(store, inner2).load() == await inner2.load()


# ---------------------------------------------------------------------------
# Conformance battery + TBox migration
# ---------------------------------------------------------------------------


async def test_conformance_ask_and_shape_checks():
    store = InMemoryGraphStore()
    await store.add([quad("ex:ada", "rdf:type", "ex:Person")])
    report = await run_conformance(
        store,
        [
            ConformanceCheck(id="has-person", ask="SELECT ?s WHERE { ?s rdf:type ex:Person . }"),
            ConformanceCheck(id="no-company", ask="SELECT ?s WHERE { ?s rdf:type ex:Company . }", min_rows=0, max_rows=0),
            ConformanceCheck(id="person-needs-name", shape=NodeShape(target_class="ex:Person", properties=[PropertyShape(path="ex:name", min_count=1)])),
        ],
    )
    by_id = {r.id: r for r in report.results}
    assert by_id["has-person"].passed is True
    assert by_id["no-company"].passed is True
    shape_check = by_id["person-needs-name"]
    assert shape_check.passed is False
    assert "ex:name" in shape_check.detail
    assert report.passed is False
    # A satisfied shape passes in isolation against the closure.
    ok = await run_conformance(store, [ConformanceCheck(id="typed", shape=NodeShape(target_class="ex:Person", properties=[PropertyShape(path="rdf:type", min_count=1)]))])
    assert ok.passed is True


async def v3_store() -> InMemoryGraphStore:
    store = InMemoryGraphStore(shapes=[NodeShape(target_class="ex:Person", properties=[PropertyShape(path="ex:name", min_count=1)])])
    await store.add(
        [
            quad("ex:Employee", "rdfs:subClassOf", "ex:Person"),
            quad("ex:ada", "rdf:type", "ex:Employee"),
            quad("ex:ada", "ex:name", '"Ada"'),
            quad("ex:ada", "ex:worksAt", "ex:acme"),
            quad("ex:acme", "ex:name", '"Acme"'),
        ]
    )
    return store


BATTERY = [ConformanceCheck(id="persons-exist", ask="SELECT ?s WHERE { ?s rdf:type ex:Employee . }")]


async def test_migrate_t_box_renames_and_keeps_old_store_intact():
    old = await v3_store()
    result = await migrate_t_box(
        old,
        TBoxMigrationSpec(
            from_="v3",
            to="v4",
            renames=[Rename(from_="ex:worksAt", to="ex:worksFor")],
            added=[quad("ex:Manager", "rdfs:subClassOf", "ex:Employee")],
        ),
        MigrationOptions(battery=BATTERY),
    )
    assert result.report.status == "migrated"
    assert result.report.rewritten_quads == 1
    assert result.report.added_quads == 1
    # Old store intact (rollback free).
    assert len(await old.match("ex:ada", "ex:worksAt", None)) == 1
    new = result.store
    assert new is not None
    assert len(await new.match("ex:ada", "ex:worksFor", "ex:acme")) == 1
    assert await new.match("ex:ada", "ex:worksAt", None) == []
    # Renamed facts keep their original derivedFrom plus the migration tag.
    renamed = (await new.match("ex:ada", "ex:worksFor", "ex:acme"))[0]
    assert "migration:v3->v4" in (renamed.derived_from or "")
    # New axiom entailed into the closure: Manager isa Person via Employee.
    assert result.report.post_battery is not None and result.report.post_battery.passed


async def test_migrate_t_box_deprecation_rewrites_and_removed_drops():
    old = await v3_store()
    result = await migrate_t_box(
        old,
        TBoxMigrationSpec(
            from_="v3",
            to="v4",
            removed=[AxiomRef(subject="ex:Employee", predicate="rdfs:subClassOf", object="ex:Person")],
            deprecations=[Deprecation(term="ex:worksAt", replacement="ex:worksFor", reason="consistency")],
        ),
        MigrationOptions(battery=[]),
    )
    assert result.report.removed_quads == 1
    assert any("ex:worksAt deprecated (consistency) — rewritten to ex:worksFor" in w for w in result.report.deprecated_warnings)
    new = result.store
    assert new is not None
    # Removed axiom: ada is no longer a Person by entailment.
    assert await new.match("ex:ada", "rdf:type", "ex:Person") == []
    assert len(await new.match("ex:ada", "ex:worksFor", "ex:acme")) == 1


async def test_migrate_t_box_version_named_graph():
    store = InMemoryGraphStore()
    await store.add(
        [
            quad("ex:Employee", "rdfs:subClassOf", "ex:Person"),
            quad("ex:worksFor", "rdfs:domain", "ex:Person"),
            quad("ex:ada", "rdf:type", "ex:Employee"),
            quad("ex:ada", "ex:worksFor", "ex:acme"),
        ]
    )
    result = await migrate_t_box(store, TBoxMigrationSpec(from_="v3", to="v4"), MigrationOptions(battery=[]))
    # Version-graph naming WITHOUT any personal name (TS uses a personal prefix).
    assert result.report.tbox_graph == "urn:framework:ontology:v4"
    v4 = result.store
    assert v4 is not None
    tbox = await v4.match(None, None, None, "urn:framework:ontology:v4")
    assert any(q.predicate == "rdfs:subClassOf" for q in tbox)
    assert any(q.predicate == "rdfs:domain" for q in tbox)
    assert all(q.subject != "ex:ada" for q in tbox)  # instance facts stay in the default graph
    assert len(await v4.match("ex:ada", "rdf:type", "ex:Employee")) == 1
    # Opt-out keeps everything graph-free.
    plain = await migrate_t_box(store, TBoxMigrationSpec(from_="v3", to="v5"), MigrationOptions(battery=[], tbox_graph=False))
    assert plain.report.tbox_graph is None
    assert plain.store is not None
    assert len(await plain.store.match(None, None, None, "urn:framework:ontology:v5")) == 0


async def test_migrate_t_box_pre_battery_aborts_post_battery_fails():
    old = await v3_store()
    plain = await migrate_t_box(old, TBoxMigrationSpec(from_="v3", to="v4"), MigrationOptions(battery=[]))
    assert plain.report.status == "migrated"
    # Impossible pre-check: no such class yet -> abort without touching.
    impossible = [ConformanceCheck(id="needs-manager", ask="SELECT ?s WHERE { ?s rdf:type ex:Manager . }", min_rows=1)]
    pre = await migrate_t_box(
        old,
        TBoxMigrationSpec(from_="v3", to="v4", added=[quad("ex:Manager", "rdfs:subClassOf", "ex:Employee")]),
        MigrationOptions(battery=impossible),
    )
    assert pre.report.status == "aborted"
    assert pre.store is None
    assert pre.report.pre_battery is not None and pre.report.pre_battery.passed is False
    # Failing post-check: renamed the class so the battery's ask finds nothing.
    post = await migrate_t_box(
        old,
        TBoxMigrationSpec(from_="v3", to="v4", renames=[Rename(from_="ex:Employee", to="ex:Staff")]),
        MigrationOptions(battery=BATTERY),
    )
    assert post.report.status == "failed"
    assert post.store is None
    # ...and the old store still satisfies the battery — nothing was destroyed.
    assert (await run_conformance(old, BATTERY)).passed is True


# ---------------------------------------------------------------------------
# Ontology review + SHACL induction
# ---------------------------------------------------------------------------


async def reviewed_store() -> InMemoryGraphStore:
    store = InMemoryGraphStore()
    await store.add(
        [
            quad("ex:Person", "rdfs:subClassOf", "ex:Thing"),
            quad("ex:Person", "rdfs:label", '"Person"'),
            quad("ex:worksFor", "rdfs:domain", "ex:Person"),  # domain but NO range
            quad("ex:name", "rdfs:range", "xsd:string"),  # range but NO domain
            quad("ex:Ghost", "rdfs:subClassOf", "ex:Thing"),  # declared, never used
            quad("ex:ada", "rdf:type", "ex:Person"),
            quad("ex:bo", "rdf:type", "ex:Person"),
            quad("ex:ada", "ex:name", '"Ada"'),
            quad("ex:bo", "ex:name", '"Bo"'),
            quad("ex:ada", "ex:worksFor", "ex:acme"),
            quad("ex:bo", "ex:worksFor", "ex:acme"),
            quad("ex:ada", "ex:nickname", '"Countess"'),  # used, never declared
        ]
    )
    return store


async def test_review_ontology_inventories_terms_and_reports_gaps():
    review = await review_ontology(await reviewed_store())
    undeclared_terms = [t.term for t in review.undeclared]
    assert "ex:nickname" in undeclared_terms
    assert "ex:name" not in undeclared_terms
    assert "ex:Ghost" in review.unused
    assert "ex:worksFor" in review.missing_range
    assert "ex:name" in review.missing_domain
    assert "rdf:type" not in [p.term for p in review.properties]
    person = next(c for c in review.classes if c.term == "ex:Person")
    assert person.label == "Person"
    assert person.uses == 2
    assert review.closure_quads >= review.explicit_quads
    # Tool surface: reviewed via the registry, camelCase keys like the TS object.
    reg = ToolRegistry().register(review_ontology_tool(await reviewed_store()))
    out = await reg.call("review_ontology", {})
    assert "ex:nickname" in [t["term"] for t in out["undeclared"]]
    assert out["closureQuads"] >= out["explicitQuads"]


async def test_propose_shapes_induces_and_coverage_threshold():
    store = await reviewed_store()
    shapes = await propose_shapes(store)
    person = next(s for s in shapes if s.target_class == "ex:Person")
    name_shape = next(p for p in person.properties if p.path == "ex:name")
    assert name_shape.min_count == 1
    assert name_shape.max_count == 1
    assert name_shape.datatype == "xsd:string"
    works_for = next(p for p in person.properties if p.path == "ex:worksFor")
    assert works_for.min_count == 1
    assert works_for.node_kind == "iri"
    assert any(p.path == "ex:nickname" for p in person.properties)  # 50% coverage -> included
    # Proposal did NOT touch the store's shape list — builder owns adoption.
    assert store.ontology_shapes() == []
    # coverage 1: every instance must have the property; nickname drops out.
    reg = ToolRegistry().register(propose_shapes_tool(store, coverage=1))
    proposals = await reg.call("propose_shapes", {})
    person_full = next(s for s in proposals if s["targetClass"] == "ex:Person")
    assert all(p["path"] != "ex:nickname" for p in person_full["properties"])
    assert any(p["path"] == "ex:name" for p in person_full["properties"])


async def test_adopt_shapes_validates_each_in_isolation():
    store = await reviewed_store()
    adoption = await adopt_shapes(
        store,
        [
            NodeShape(target_class="ex:Person", properties=[PropertyShape(path="ex:name", min_count=1)]),
            NodeShape(target_class="ex:Person", properties=[PropertyShape(path="ex:nickname", min_count=1)]),
        ],
    )
    assert [s.target_class for s in adoption.adopted] == ["ex:Person"]
    assert len(adoption.rejected) == 1
    assert "ex:bo" in adoption.rejected[0].violations[0]
    # The adopted shape now enforces future writes; the hostile one was not registered.
    assert len(store.ontology_shapes()) == 1
    with pytest.raises(ValueError, match="SHACL violation"):
        await store.add([quad("ex:cyd", "rdf:type", "ex:Person",), quad("ex:cyd", "ex:nickname", '"C"')])


# ---------------------------------------------------------------------------
# Remote SPARQL adapter (httpx, MockTransport)
# ---------------------------------------------------------------------------


RESULTS_JSON = {
    "head": {"vars": ["iri", "name"]},
    "results": {
        "bindings": [
            {"iri": {"value": "http://example.org/Ada"}, "name": {"value": "Ada Lovelace"}},
            {"iri": {"value": "http://example.org/Bo"}, "name": {"value": "Bo"}},
        ]
    },
}


def make_adapter(handler, **kw) -> RemoteSparqlAdapter:
    def fetch_fn() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    defaults = {"endpoint": "https://sparql.example.org/sparql/"}
    defaults.update(kw)
    return RemoteSparqlAdapter(fetch_fn=fetch_fn, **defaults)


async def test_remote_select_posts_query_parses_rows_and_pins_prefixes():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = request.content.decode()
        return httpx.Response(200, json=RESULTS_JSON)

    adapter = make_adapter(
        handler,
        auth=SparqlAuth(type="bearer", token="tok"),
        prefixes={"wd": "http://www.wikidata.org/entity/"},
    )
    rows = await adapter.select("SELECT ?iri ?name WHERE { ?iri ?p ?name . }")
    assert rows == [
        {"iri": "http://example.org/Ada", "name": "Ada Lovelace"},
        {"iri": "http://example.org/Bo", "name": "Bo"},
    ]
    assert seen["url"] == "https://sparql.example.org/sparql"  # trailing slash stripped
    headers = seen["headers"]
    assert headers["authorization"] == "Bearer tok"
    assert "PREFIX wd: <http://www.wikidata.org/entity/>" in str(seen["body"])
    assert "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>" in str(seen["body"])
    assert "SELECT ?iri ?name" in str(seen["body"])
    assert headers["content-type"] == "application/sparql-query"
    assert headers["accept"] == "application/sparql-results+json"


async def test_remote_default_user_agent_has_no_personal_name():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["user-agent"] = request.headers.get("user-agent")
        return httpx.Response(200, json={"head": {"vars": []}, "results": {"bindings": []}})

    await make_adapter(handler).select("SELECT ?x WHERE { ?s ?p ?x }")
    ua = seen["user-agent"]
    assert ua == "agent-framework/0.1 (+RemoteSparqlAdapter)"
    assert "fabisch" not in ua


async def test_remote_retries_429_honoring_retry_after():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "0.1"}, text="")
        return httpx.Response(
            200, json={"head": {"vars": ["x"]}, "results": {"bindings": [{"x": {"value": "urn:a"}}]}}
        )

    adapter = make_adapter(handler, max_retries=2)
    start = time.monotonic()
    rows = await adapter.select("SELECT ?x WHERE { ?s ?p ?x }")
    elapsed = time.monotonic() - start
    assert len(calls) == 2
    assert rows == [{"x": "urn:a"}]
    assert elapsed >= 0.08  # honored the ~0.1s Retry-After before retrying


async def test_remote_non_retryable_4xx_fails_immediately():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, text="bad query")

    adapter = make_adapter(handler)
    with pytest.raises(AgentError) as exc_info:
        await adapter.select("SELECT ?x WHERE { broken")
    assert exc_info.value.code == "REMOTE_SPARQL"
    assert exc_info.value.retryable is False
    assert len(calls) == 1  # exactly one attempt, no retry loop


async def test_remote_update_posts_sparql_update_to_update_endpoint():
    posted: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append((str(request.url), request.headers.get("content-type", "")))
        if len(posted) == 1:
            return httpx.Response(200)
        return httpx.Response(400, text="bad update syntax")

    adapter = make_adapter(handler, update_endpoint="https://sparql.example.org/update")
    await adapter.update("INSERT DATA { <urn:a> <urn:b> <urn:c> }")
    assert posted == [("https://sparql.example.org/update", "application/sparql-update")]
    with pytest.raises(AgentError) as exc_info:
        await adapter.update("INSERT DATA { x }")
    assert exc_info.value.code == "REMOTE_SPARQL"
    assert exc_info.value.retryable is False
    assert len(posted) == 2  # both updates were single attempts


async def test_remote_tool_names_and_read_only_default():
    adapter = make_adapter(lambda request: httpx.Response(200, json=RESULTS_JSON))
    assert remote_sparql_tool(adapter).name == "remote_sparql_select"
    assert remote_sparql_tool(adapter, allow_update=True).name == "remote_sparql"
    reg = ToolRegistry().register(remote_sparql_tool(adapter))
    rows = await reg.call("remote_sparql_select", {"query": "SELECT ?iri ?name WHERE { ?iri ?p ?name . }"})
    assert rows[0]["name"] == "Ada Lovelace"