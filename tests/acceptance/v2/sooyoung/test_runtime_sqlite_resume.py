"""Actual runtime store/SQLite restart of a real web worker's pending result.

Run in the combined runtime candidate; PR main does not yet contain the
runtime owner's modules. HTTP and LLM replies are deterministic, while the
worker, RAG resolver, file store, Supervisor, graph and SQLite saver are real.
"""
import asyncio
from urllib.parse import urlsplit

import httpx
import pytest

runtime_store = pytest.importorskip("main.storage.artifacts", reason="requires Juhyeon runtime integration")
runtime_checkpoints = pytest.importorskip("main.storage.checkpoints", reason="requires Juhyeon runtime integration")

from main.agents.eligibility.agent import EligibilityAgent
from main.agents.supervisor import contracts as c
from main.agents.supervisor.agent import Supervisor
from main.graph.supervisor_workflow import build_supervisor_graph, initialize_run
from main.rag.neutral.search import LegacySearchProvider, WebSourceMetadata
from main.rag.neutral.tools import NeutralResearchTools, StoredEvidenceResolver
from tests.acceptance.v2.sooyoung.test_web_resolver_integration import (
    AS_OF, DOMAIN, NOW, DeterministicLLM, DiscoveryBackend, EmptyRetriever,
)


@pytest.mark.parametrize("case,expected_verdict,terminal_reason", [
    ("eligible", "eligible", None),
    ("mixed_date", "unknown", "ELIGIBILITY_UNKNOWN"),
    ("stage_range", "unknown", "ELIGIBILITY_UNKNOWN"),
    ("current_d", "ineligible", "INELIGIBLE_TARGET"),
])
def test_pending_web_result_resumes_from_actual_sqlite_without_recalling_worker(
    tmp_path, case, expected_verdict, terminal_reason,
):
    async def scenario():
        artifact_root = tmp_path / "artifacts"
        store = runtime_store.FileArtifactStore(artifact_root)
        config = c.RunConfig(supervisor_model="fixture", research_model="fixture",
            report_model="fixture", judge_model="fixture", prompt_hash="a" * 64,
            corpus_version="web-fixture-v1", max_eligibility_attempts=1)
        run_input = c.RunInput(company_name="Acme", official_url="https://acme.example",
            domain=DOMAIN, as_of_date=AS_OF, corpus_manifest=[])
        initial = await initialize_run(run_input, config, store=store, now=lambda: NOW)
        company_id = (await store.load(initial["company_ref"]))["company_id"]
        resolver = StoredEvidenceResolver(store=store, company_id=company_id, domain=DOMAIN)
        unlisted = ("Acme is unlisted as of 2025-01-01 and released its latest product on 2026-10-13."
            if case == "mixed_date" else "Acme is unlisted as of 2026-10-13.")
        stage = ("Series C/D" if case == "stage_range" else
            "Series C, but is now Series D" if case == "current_d" else "Series B")
        body = f"{unlisted}\nAcme current funding stage is {stage} as of 2026-10-13.\nAcme has not completed an exit as of 2026-10-13."
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
            httpx.Response(200, text=body, headers={"content-type": "text/plain; charset=utf-8"})))
        provider = LegacySearchProvider(backend=DiscoveryBackend(), store=store,
            company_id=company_id, company_aliases=["Acme"], domain=DOMAIN, client=client,
            classify_source=lambda hit: WebSourceMetadata(source_type="institution",
                publisher="Original Register", publisher_group=urlsplit(str(hit.url)).hostname,
                published_at=AS_OF, corporate_controlled=False))
        tools = NeutralResearchTools(retriever=EmptyRetriever(), search_provider=provider)
        real_worker = EligibilityAgent(tools=tools, llm=DeterministicLLM(), domain=DOMAIN,
            model="fixture", store=store, resolver=resolver)
        calls = {"eligibility": 0, "writer": 0}

        async def eligibility(request):
            calls["eligibility"] += 1
            return await real_worker(request)

        async def writer(request):
            calls["writer"] += 1
            raise AssertionError("No eligible evidence gate can exist in this scenario")

        async def unused(request):
            raise AssertionError("Restart check stops before the next business worker")

        async def assessor(request):
            return []

        workers = {node: unused for node in
            ("eligibility", "writer", "evaluator", *(p.value for p in c.PERSPECTIVES))}
        workers.update(eligibility=eligibility, writer=writer)
        checkpoint_path = tmp_path / "checkpoints.sqlite"
        invocation = {"configurable": {"thread_id": initial["thread_id"]}}
        async with client:
            async with runtime_checkpoints.open_checkpointer(checkpoint_path) as saver:
                supervisor = Supervisor(store, resolver, assessor, now=lambda: NOW, limits=config)
                graph = build_supervisor_graph(supervisor=supervisor, workers=workers,
                    publish_worker=unused, store=store, run_config=config,
                    checkpointer=saver, now=lambda: NOW)
                paused = await graph.ainvoke(initial, invocation, interrupt_after=["eligibility"])
                assert paused["pending_result"].outcome == "success"
                task_id = paused["pending_result"].task_id
                result_ref = paused["pending_result"].result_ref
                result = c.EligibilityResult.model_validate(await store.load(result_ref))
                assert result.verdict == expected_verdict
                assert calls == {"eligibility": 1, "writer": 0}
                registry_ref = await store.save({
                    "source_refs": {key: ref.model_dump(mode="json") for key, ref in resolver.source_refs.items()},
                    "evidence_refs": {key: ref.model_dump(mode="json") for key, ref in resolver.evidence_refs.items()},
                }, key="registries/sooyoung-evidence.json")

            # Close the connection and rebuild service objects. Registry refs
            # are decoded from file bytes, never reused from live dictionaries.
            resumed_store = runtime_store.FileArtifactStore(artifact_root, run_id=initial["run_id"])
            registry = await resumed_store.load(registry_ref)
            resumed_resolver = StoredEvidenceResolver(store=resumed_store,
                company_id=company_id, domain=DOMAIN,
                source_refs={key: c.ArtifactRef.model_validate(value) for key, value in registry["source_refs"].items()},
                evidence_refs={key: c.ArtifactRef.model_validate(value) for key, value in registry["evidence_refs"].items()})
            evidence_ids = list(dict.fromkeys(eid for claim in result.claims for eid in claim.evidence_ids))
            assert (await resumed_resolver.resolve(evidence_ids)).evidence
            async with runtime_checkpoints.open_checkpointer(checkpoint_path) as saver:
                restored = await runtime_checkpoints.load_resume_state(saver,
                    initial["thread_id"], initial["versions"])
                assert restored["pending_result"].result_ref == result_ref
                supervisor = Supervisor(resumed_store, resumed_resolver, assessor,
                    now=lambda: NOW, limits=config)
                graph = build_supervisor_graph(supervisor=supervisor, workers=workers,
                    publish_worker=unused, store=resumed_store, run_config=config,
                    checkpointer=saver, now=lambda: NOW)
                resumed = await graph.ainvoke(None, invocation, interrupt_after=["supervisor"])
                assert calls == {"eligibility": 1, "writer": 0}
                assert resumed["accepted_task_ids"].count(task_id) == 1
                assert resumed["eligibility_ref"] == result_ref
                assert resumed["eligibility_status"] == expected_verdict
                assert resumed["pending_result"] is None and resumed["final_manifest_ref"] is None
                if terminal_reason:
                    assert resumed["status"] == "incomplete"
                    assert resumed["terminal_reason"] == terminal_reason
                else:
                    assert resumed["status"] == "running"
                    assert resumed["active_task"].node.value in {p.value for p in c.PERSPECTIVES}
        assert checkpoint_path.read_bytes().startswith(b"SQLite format 3")
    asyncio.run(scenario())
