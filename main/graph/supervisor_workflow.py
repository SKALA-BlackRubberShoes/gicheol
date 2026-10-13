"""Injectable serial Supervisor graph for the SDD 1.0 neutral evaluation."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph

from main.agents.supervisor.contracts import (
    ArtifactStore, NextAction, PERSPECTIVES, RunConfig, RunInput, canonical_hash,
)
from main.agents.supervisor.agent import Supervisor, bounded_store
from .supervisor_nodes import make_abort_node, make_publish_node, make_worker_node, utc_now
from .supervisor_state import (
    StateIntegrityError, SupervisorState, new_state, validate_run_settings, validate_state,
)


def route_after_supervisor(state: SupervisorState) -> str:
    """Return one action without mutating State or scheduling peer workers."""
    return NextAction(state["next_action"]).value


async def initialize_run(
    run_input: RunInput, run_config: RunConfig, *, store: ArtifactStore,
    now: Callable[[], datetime] = utc_now,
) -> SupervisorState:
    run_input = RunInput.model_validate(run_input.model_dump(mode="json"))
    run_config = RunConfig.model_validate(run_config.model_dump(mode="json"))
    started_at = now()
    validate_run_settings(run_input, run_config, started_at)
    company = {
        "company_id": canonical_hash({"company_name": run_input.company_name,
                                      "official_url": str(run_input.official_url)}),
        "company_name": run_input.company_name, "official_url": str(run_input.official_url),
        "domain": run_input.domain.value, "as_of_date": run_input.as_of_date.isoformat(),
    }
    io = bounded_store(store, {"deadline_at": started_at + timedelta(seconds=run_config.run_deadline_seconds)},
                       limits=run_config, now=now)
    # The store instance is scoped to a single run's artifact root.
    input_ref = await io.save(run_input.model_dump(mode="json"), key="snapshots/input.json")
    config_ref = await io.save(run_config.model_dump(mode="json"), key="snapshots/config.json")
    company_ref = await io.save(company, key="snapshots/company.json")
    return new_state(run_input, run_config, input_ref=input_ref, config_ref=config_ref,
                     company_ref=company_ref, now=started_at)


def validate_resume_versions(state: SupervisorState, current_versions: Mapping[str, str]) -> None:
    validate_state(state)
    if state["status"] != "running":
        raise StateIntegrityError("terminal run cannot restart with the same run_id",
                                  "RESUME_VERSION_MISMATCH")
    if dict(current_versions) != state["versions"]:
        raise StateIntegrityError("resume requires the original schema/corpus/index/config/prompt/policy/model",
                                  "RESUME_VERSION_MISMATCH")


def build_supervisor_graph(
    *, supervisor: Any, workers: Mapping[str, Callable], publish_worker: Callable,
    store: ArtifactStore, run_config: RunConfig, checkpointer: Any,
    now: Callable[[], datetime] = utc_now,
):
    """Compile registered DTO workers with an explicitly supplied checkpointer.

    Production injects the runtime owner's SQLite checkpointer. Tests may supply
    an explicit in-memory checkpointer; there is no default ephemeral fallback.
    """
    if checkpointer is None:
        raise ValueError("An explicit checkpointer must be injected")
    expected = {"eligibility", "writer", "evaluator", *(p.value for p in PERSPECTIVES)}
    if set(workers) != expected:
        raise ValueError("worker registrations must include exactly the seven business workers")
    graph = StateGraph(SupervisorState)

    def initialize(state: SupervisorState) -> SupervisorState:
        checked = validate_state(state, max_graph_steps=run_config.max_graph_steps)
        if checked["status"] != "running":
            raise StateIntegrityError("terminal run cannot be invoked as a new execution")
        if checked["versions"]["config"] != canonical_hash(run_config.model_dump(mode="json")):
            raise StateIntegrityError("compiled graph config differs from frozen run config",
                                      "RESUME_VERSION_MISMATCH")
        return checked

    async def supervise(state: SupervisorState) -> SupervisorState:
        initialize(state)
        if isinstance(supervisor, Supervisor):
            return await supervisor.step(state, limits=run_config)
        return await supervisor.step(state)

    graph.add_node("initialize", initialize)
    graph.add_node("supervisor", supervise)
    for node, worker in workers.items():
        graph.add_node(node, make_worker_node(node, worker, store=store, now=now, limits=run_config))
        graph.add_edge(node, "supervisor")
    graph.add_node("publish", make_publish_node(publish_worker, store=store, now=now, limits=run_config))
    graph.add_node("abort", make_abort_node(store=store, now=now, limits=run_config))
    graph.add_edge(START, "initialize")
    graph.add_edge("initialize", "supervisor")
    graph.add_conditional_edges("supervisor", route_after_supervisor,
                                {action.value: action.value for action in NextAction})
    graph.add_edge("publish", END)
    graph.add_edge("abort", END)
    return graph.compile(checkpointer=checkpointer).with_config(
        {"recursion_limit": run_config.recursion_limit})
