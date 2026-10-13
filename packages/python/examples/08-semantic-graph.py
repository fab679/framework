"""LIVE: semantic graph — a live model commits validated facts, the ontology
self-reports gaps, SHACL is induced from ITS OWN data and adopted."""
import asyncio

from framework_agents import Agent, AgentOptions, InMemoryGraphStore, define_tool, init_chat_model, load_config
from framework_agents.middleware import model_retry
from framework_agents.semantic import graph_add_tool, claim_check_tool, sparql_select_tool
from framework_agents.ontology_review import propose_shapes, adopt_shapes, review_ontology, review_ontology_tool, propose_shapes_tool

cfg = load_config()

async def main():
    store = InMemoryGraphStore()
    model = lambda: init_chat_model(f"together:{cfg.together_model}", max_tokens=1500)
    writer = Agent(AgentOptions(
        model=model(), max_steps=8,
        middleware=[model_retry(max_retries=2, initial_delay_ms=300)],
        tools=[graph_add_tool(store), review_ontology_tool(store), propose_shapes_tool(store, min_instances=1)],
    ))
    run = await writer.run(
        'Call graph_add once with these 6 quads (source "user:demo", keep quotes): '
        "1. ex:GlobalCorp rdf:type ex:Company ; 2. ex:GlobalCorp ex:name \"Global Corp\" ; 3. ex:GlobalCorp ex:headcount 42 ; "
        "4. ex:Acme rdf:type ex:Company ; 5. ex:Acme ex:name \"Acme\" ; 6. ex:Acme ex:headcount 10 . Reply with just: stored"
    )
    print("WRITE:", run.text.strip()[:40])
    review = await review_ontology(store)
    undeclared = list(review.undeclared) if review.undeclared else []
    print("UNDECLARED (what to add next):", [getattr(t, "term", t.get("term") if isinstance(t, dict) else t) for t in undeclared])
    shapes = await propose_shapes(store, min_instances=1)
    adoption = await adopt_shapes(store, shapes)
    print("SHACL INDUCED:", bool(shapes), "| adopted:", len(adoption.adopted), "rejected:", len(adoption.rejected))
    reader = Agent(AgentOptions(model=model(), max_steps=4, tools=[claim_check_tool(store), sparql_select_tool(store)]))
    answer = await reader.run('How many employees does GlobalCorp have? Ground it with claim_check or sparql_select first.')
    print("GROUNDED:", answer.text.strip()[:80])
    ok = "42" in answer.text and adopted
    print("PASS: graph write + review + SHACL adoption + grounded answer" if ok else "NOTE: model variance — inspect")

asyncio.run(main())
