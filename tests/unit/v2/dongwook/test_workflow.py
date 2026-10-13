"""Graph registration follows state routing and all workers return to Supervisor."""
import asyncio

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from main.agents.supervisor.contracts import PERSPECTIVES, RunConfig, RunInput
from main.graph.supervisor_workflow import (
    build_supervisor_graph, initialize_run, route_after_supervisor,
)
from test_state import make_state, NOW
from test_nodes import Store


def test_router_is_pure_and_selects_from_current_action():
    state = make_state()
    state["next_action"] = "market"
    before = state.copy()
    assert route_after_supervisor(state) == "market"
    assert state == before
    state["next_action"] = "technology_maturity"
    assert route_after_supervisor(state) == "technology_maturity"


def test_all_business_workers_return_to_supervisor_without_peer_edges():
    class Supervisor:
        async def step(self, state):
            return state
    async def service(request):
        raise AssertionError("this test inspects boundary registration only")
    store = Store()
    workers = {name: service for name in ("eligibility", "writer", "evaluator",
                                         *(p.value for p in PERSPECTIVES))}
    graph = build_supervisor_graph(
        supervisor=Supervisor(), workers=workers, publish_worker=service,
        store=store, run_config=RunConfig(), checkpointer=InMemorySaver(),
    )
    edges = graph.get_graph().edges
    worker_names = set(workers)
    assert all({e.target for e in edges if e.source == name} == {"supervisor"}
               for name in worker_names)
    assert all(e.conditional for e in edges if e.source == "supervisor")
    assert not any(e.source in worker_names and e.target in worker_names for e in edges)


def test_graph_requires_explicit_checkpointer_and_all_worker_registrations():
    async def service(request):
        return request
    class Supervisor:
        async def step(self, state):
            return state
    with pytest.raises(ValueError, match="checkpointer"):
        build_supervisor_graph(supervisor=Supervisor(), workers={},
            publish_worker=service, store=Store(), run_config=RunConfig(), checkpointer=None)
    with pytest.raises(ValueError, match="worker"):
        build_supervisor_graph(supervisor=Supervisor(), workers={},
            publish_worker=service, store=Store(), run_config=RunConfig(),
            checkpointer=InMemorySaver())


def test_initialize_validates_before_writes_and_keeps_only_refs():
    async def scenario():
        store = Store()
        data = RunInput(company_name="Acme", official_url="https://acme.test",
                        domain="Physical AI / Robotics", as_of_date="2026-10-13",
                        corpus_manifest=[])
        with pytest.raises(ValueError, match="model"):
            await initialize_run(data, RunConfig(), store=store, now=lambda: NOW)
        assert store.items == {}
        config = RunConfig(supervisor_model="s", research_model="r", report_model="w",
                           judge_model="j", prompt_hash="b" * 64)
        state = await initialize_run(data, config, store=store, now=lambda: NOW)
        assert not any(key in state for key in ("company_name", "corpus_manifest", "raw_documents"))
        company = await store.load(state["company_ref"])
        assert company["company_name"] == "Acme"
        assert len(company["company_id"]) == 64
        assert state["config_ref"] != state["input_ref"]
    asyncio.run(scenario())
