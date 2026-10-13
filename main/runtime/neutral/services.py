"""Explicit service assembly; owner services are never silently mocked."""
from __future__ import annotations

import importlib
import inspect
import resource
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from main.agents.supervisor import contracts as c
from main.agents.supervisor.agent import ServiceTimeout, Supervisor
from main.graph.supervisor_workflow import build_supervisor_graph


class ServiceConfigurationError(ValueError):
    code = "CONFIG_OR_AUTH_ERROR"


@dataclass(frozen=True)
class ServiceBundle:
    """Composition container, not a second set of business DTOs.

    Factories receive runtime, store, llm. Every worker consumes the single
    request DTO defined in contracts.py. Optional ingestion/retrieval adapters
    receive named keyword arguments documented in interfaces.md.
    """
    workers: Mapping[str, Callable]
    evidence_resolver: c.EvidenceResolver
    assess_evidence: Callable
    publish_worker: Callable
    ingest: Callable | None = None
    retrieve: Callable | None = None
    mock: bool = False

    def validate(self) -> None:
        if type(self.mock) is not bool:
            raise ServiceConfigurationError("ServiceBundle.mock must be an explicit boolean")
        expected = {"eligibility", "writer", "evaluator", *(p.value for p in c.PERSPECTIVES)}
        if set(self.workers) != expected:
            raise ServiceConfigurationError("Register exactly the seven SDD business workers")
        for callback in [*self.workers.values(), self.assess_evidence, self.publish_worker,
                         getattr(self.evidence_resolver, "resolve", None),
                         *[optional for optional in (self.ingest, self.retrieve) if optional is not None]]:
            if not (inspect.iscoroutinefunction(callback) or
                    inspect.iscoroutinefunction(getattr(callback, "__call__", None))):
                raise ServiceConfigurationError("All service boundaries must be async")


def unconfigured_services(**_: Any) -> ServiceBundle:
    raise ServiceConfigurationError(
        "Neutral owner services are not registered. Set services_factory to a factory "
        "returning ServiceBundle: Suhyeon RAG/technology; Sooyoung eligibility/research; "
        "Hyojin sufficiency/evaluator; Woonghee writer/publish. See docs/v2/integration/interfaces.md."
    )


async def load_services(runtime, store, llm) -> ServiceBundle:
    target = runtime.services_factory
    module, separator, attribute = target.partition(":")
    if not separator or not module or not attribute or "." in attribute:
        raise ServiceConfigurationError("services_factory must be module:function")
    try:
        factory = getattr(importlib.import_module(module), attribute)
    except (ImportError, AttributeError) as exc:
        raise ServiceConfigurationError(f"Cannot load service factory {target}") from exc
    value = factory(runtime=runtime, store=store, llm=llm)
    bundle = await value if inspect.isawaitable(value) else value
    if not isinstance(bundle, ServiceBundle):
        raise ServiceConfigurationError("Service factory must return ServiceBundle")
    bundle.validate()
    return bundle


def assemble_graph(*, bundle, store, run_config, checkpointer, recorder=None,
                   deadline_at=None, now=None):
    """Inject production storage/checkpointer into Dongwook's unchanged graph."""
    from .llm import call_scope
    from .errors import RuntimeFailure
    from .retries import RetryBudget

    bundle.validate()
    def wrap(node, callback):
        async def invoke(request):
            with call_scope(deadline_at=deadline_at, node=node,
                            budget=RetryBudget(max_retries=run_config.max_transient_retries)):
                if recorder is None:
                    return await callback(request)
                async with recorder.node_span(node, task_id=request.task_id,
                        perspective=getattr(request, "perspective", None),
                        attempt=getattr(request, "attempt", 1),
                        gap_ids=getattr(request, "gap_ids", ())):
                    return await callback(request)
        return invoke

    async def assess(request):
        # SufficiencyRequest intentionally has no worker task_id. Bind its
        # immutable request to a stable tracing ID and the original deadline.
        task_id = "sufficiency-" + c.canonical_hash(request)
        with call_scope(deadline_at=deadline_at, node="assess_evidence",
                        budget=RetryBudget(max_retries=run_config.max_transient_retries)):
            try:
                if recorder is None:
                    result = await bundle.assess_evidence(request)
                else:
                    async with recorder.node_span("assess_evidence", task_id=task_id):
                        result = await bundle.assess_evidence(request)
            except RuntimeFailure as exc:
                # The existing Supervisor recognizes its timeout boundary but
                # otherwise treats assessor exceptions as invalid model output.
                # Preserve the original run budget's incomplete classification.
                if exc.error.code == c.TerminalReason.BUDGET_EXHAUSTED:
                    raise ServiceTimeout(True) from None
                raise
            verdicts = [c.SufficiencyVerdict.model_validate(verdict) for verdict in result]
            if recorder is not None:
                await recorder.event("evidence_gate_assessment", task_id=task_id,
                    gate_verdict=(len(verdicts) == len(c.PERSPECTIVES)
                                  and {v.perspective for v in verdicts} == set(c.PERSPECTIVES)
                                  and all(v.structural_pass and v.semantic_pass for v in verdicts)),
                    evidence_ids=sorted({identifier for verdict in verdicts for identifier in verdict.evidence_ids}),
                    gap_ids=sorted({gap.gap_id for verdict in verdicts for gap in verdict.gaps}))
            return verdicts
    options = {"now": now} if now is not None else {}
    supervisor = Supervisor(store, bundle.evidence_resolver, assess,
                            decision_sink=recorder, limits=run_config, **options)
    return build_supervisor_graph(supervisor=supervisor,
        workers={node: wrap(node, callback) for node, callback in bundle.workers.items()},
        publish_worker=wrap("publish", bundle.publish_worker), store=store,
        run_config=run_config, checkpointer=checkpointer, **options)


async def evaluate_retrieval(bundle, dataset: dict, *, top_k: int = 5) -> dict:
    """Measure actual adapter results; gold labels never stand in for retrieval."""
    if bundle.retrieve is None:
        raise ServiceConfigurationError("No neutral retrieval adapter is registered")
    if top_k != 5:
        raise ValueError("The fixed retrieval acceptance evaluation requires top_k=5")
    queries = dataset.get("queries")
    if not isinstance(queries, list) or len(queries) != 20:
        raise ValueError("Retrieval gold requires exactly 20 labeled queries")
    for query in queries:
        if (not isinstance(query, dict) or not isinstance(query.get("query_id"), str) or not query["query_id"].strip()
            or not isinstance(query.get("query"), str) or not query["query"].strip()
            or not isinstance(query.get("relevant_source_ids"), list) or not query["relevant_source_ids"]
            or any(not isinstance(source, str) or not source.strip() for source in query["relevant_source_ids"])):
            raise ValueError("Each query needs an ID, text and a list of human-labeled relevant sources")
    if {p.value: sum(q.get("perspective") == p.value for q in queries) for p in c.PERSPECTIVES} != {p.value: 5 for p in c.PERSPECTIVES}:
        raise ValueError("Retrieval gold requires five queries per perspective")
    if sum(q.get("language") == "ko" for q in queries) != 10 or sum(q.get("language") == "en" for q in queries) != 10:
        raise ValueError("Retrieval gold requires ten Korean and ten English queries")
    if len({q.get("query_id") for q in queries}) != 20:
        raise ValueError("Gold query IDs must be unique")
    rows = []
    for query in queries:
        started = time.perf_counter()
        request_query = {key: value for key, value in query.items() if key != "relevant_source_ids"}
        result = await bundle.retrieve(query=request_query, top_k=top_k)
        result = c.RetrievalResult.model_validate(result)
        if result.receipt.outcome not in {"success", "empty"}:
            raise ServiceConfigurationError("Retrieval provider failed; metrics are not valid")
        if result.receipt.query_id != query["query_id"] or result.receipt.query_text != query["query"]:
            raise ServiceConfigurationError("Retrieval receipt does not belong to the evaluated query")
        hits = sorted((hit for hit in result.receipt.ranked_hits if hit.rank <= top_k), key=lambda hit: hit.rank)
        rank = next((hit.rank for hit in hits
                     if hit.source_id in query["relevant_source_ids"]), None)
        rows.append({"query_id": query["query_id"], "hit": rank is not None,
                     "perspective": query["perspective"], "language": query["language"],
                     "reciprocal_rank": 1 / rank if rank else 0,
                     "latency_seconds": time.perf_counter() - started})
    latencies = sorted(row["latency_seconds"] for row in rows)
    def percentile(fraction):
        index = fraction * (len(latencies) - 1)
        lower = int(index)
        upper = min(lower + 1, len(latencies) - 1)
        return latencies[lower] + (latencies[upper] - latencies[lower]) * (index - lower)
    by_perspective = {}
    for perspective in c.PERSPECTIVES:
        group = [row for row in rows if row["perspective"] == perspective.value]
        by_perspective[perspective.value] = {
            "query_count": len(group), "hit_rate_at_5": sum(row["hit"] for row in group) / len(group),
            "mrr_at_5": sum(row["reciprocal_rank"] for row in group) / len(group)}
    peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return {"mock": bundle.mock, "query_count": len(rows), "top_k": top_k,
            "hit_rate_at_5": sum(row["hit"] for row in rows) / len(rows),
            "mrr_at_5": sum(row["reciprocal_rank"] for row in rows) / len(rows),
            "mean_latency_seconds": sum(row["latency_seconds"] for row in rows) / len(rows),
            "p50_latency_seconds": percentile(0.50), "p95_latency_seconds": percentile(0.95),
            "peak_memory_bytes": peak_rss if sys.platform == "darwin" else peak_rss * 1024,
            "memory_measurement": "process_lifetime_peak_rss", "by_perspective": by_perspective,
            "queries": rows}
