"""SDD graph wiring and publish/replay contracts with deterministic service fixtures.

These are orchestration acceptance checks, not live RAG, Judge, or real PDF tests.
"""
import asyncio
from datetime import timedelta
from pathlib import Path
import sys

from langgraph.checkpoint.memory import InMemorySaver

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "unit/v2/dongwook"))
from policy_fixtures import (
    NOW, MemoryStore, Resolver, assessor, contracts, draft, evaluation,
    full_fixture, new_run, supervisor_api,
)
from main.graph.supervisor_nodes import make_publish_node
from main.graph.supervisor_workflow import build_supervisor_graph


class PublishStore(MemoryStore):
    def __init__(self):
        super().__init__()
        self.manifests = {}
        self.fail_commit = False
        self.crash_after_commit = False

    async def find_manifest(self, run_id, report_revision):
        return self.manifests.get((run_id, report_revision))

    async def commit_manifest(self, manifest):
        if self.fail_commit:
            raise OSError("simulated disk failure")
        ref = await super().commit_manifest(manifest)
        self.manifests[manifest.run_id, manifest.report_revision] = ref
        if self.crash_after_commit:
            self.crash_after_commit = False
            raise RuntimeError("simulated checkpoint window")
        return ref


async def fixture():
    original, state = await new_run()
    store = PublishStore()
    store.payloads.update(original.payloads)
    results, verdicts, bundle, _, _ = await full_fixture(store, state)
    supervisor = supervisor_api().Supervisor(
        store, Resolver(bundle), await assessor(verdicts), now=lambda: NOW)
    config = contracts().RunConfig.model_validate(await store.load(state["config_ref"]))
    return store, state, supervisor, config


async def publish_fixture(store, request):
    c = contracts()
    pdf = await store.save({"fixture": "PDF renderer validation boundary"},
                           key=f"staging/report-{request.report.revision}.pdf",
                           media_type="application/pdf")
    return c.PublishResult(
        report_revision=request.report.revision, report_hash=request.report.content_hash,
        evidence_epoch=request.report.evidence_epoch, staging_pdf_ref=pdf,
        pdf_validation_pass=True, page_count=1, summary_page_fraction=0.4,
    )


async def ready_to_publish():
    store, state, supervisor, config = await fixture()
    state = await supervisor.step(state)
    report = draft(state)
    state["pending_result"] = contracts().ResultEnvelope(
        task_id=state["active_task"].task_id, outcome="success",
        result_ref=await store.save(report, key="draft.json"))
    state = await supervisor.step(state)
    state["pending_result"] = contracts().ResultEnvelope(
        task_id=state["active_task"].task_id, outcome="success",
        result_ref=await store.save(evaluation(report), key="evaluation.json"))
    return store, await supervisor.step(state), config


def test_at03_actual_graph_runs_serial_dto_workers_and_commits_current_manifest():
    async def scenario():
        store, state, supervisor, config = await fixture()
        async def unused(request):
            raise AssertionError("Sufficient views and eligible target cannot be re-called")
        async def writer(request):
            c = contracts()
            assert set(request.perspective_results) == set(c.PERSPECTIVES)
            sections, claim_ids, citations = [], [], []
            for section in c.SECTION_ORDER:
                paragraphs = []
                if section.value in {p.value for p in c.PERSPECTIVES}:
                    for claim in request.perspective_results[section.value].claims:
                        paragraph_id = "paragraph-" + claim.claim_id
                        paragraphs.append(c.ReportParagraph(
                            paragraph_id=paragraph_id, text=claim.text, kind=claim.kind,
                            claim_ids=[claim.claim_id], evidence_ids=claim.evidence_ids))
                        claim_ids.append(claim.claim_id)
                        for evidence_id in claim.evidence_ids:
                            item = next(e for e in request.evidence if e.evidence_id == evidence_id)
                            citations.append(c.Citation(source_id=item.source_id,
                                evidence_id=evidence_id, paragraph_id=paragraph_id))
                sections.append(c.ReportSection(section_id=section, title=section.value,
                                                paragraphs=paragraphs))
            return c.DraftReport(revision=request.report_revision,
                evidence_epoch=request.evidence_epoch, sections=sections,
                claim_ids=claim_ids, citations=citations)
        async def judge(request):
            return evaluation(request.report)
        async def publish(request):
            return await publish_fixture(store, request)
        workers = {node: unused for node in ("eligibility", *(p.value for p in contracts().PERSPECTIVES))}
        workers.update(writer=writer, evaluator=judge)
        graph = build_supervisor_graph(supervisor=supervisor, workers=workers,
            publish_worker=publish, store=store, run_config=config,
            checkpointer=InMemorySaver(), now=lambda: NOW)
        result = await graph.ainvoke(state, {"configurable": {"thread_id": state["thread_id"]}})
        assert result["status"] == "completed"
        manifest = contracts().Manifest.model_validate(await store.load(result["final_manifest_ref"]))
        assert manifest.report_revision == result["report_revision"]
        assert manifest.evidence_epoch == result["evidence_epoch"]
        assert len(result["accepted_task_ids"]) == 2
        assert all(result["perspectives"][p].result_hash == state["perspectives"][p].result_hash
                   for p in contracts().PERSPECTIVES)
    asyncio.run(scenario())


def test_at19_manifest_commit_failure_preserves_draft_and_writes_diagnostics():
    async def scenario():
        store, state, config = await ready_to_publish()
        store.fail_commit = True
        async def publish(request):
            return await publish_fixture(store, request)
        result = await make_publish_node(publish, store=store, limits=config,
                                        now=lambda: NOW)(state)
        assert result["status"] == "failed"
        assert result["final_manifest_ref"] is None
        assert result["draft_report_ref"] == state["draft_report_ref"]
        assert store.payloads["diagnostics.json"]["status"] == "failed"
    asyncio.run(scenario())


def test_at19_commit_before_checkpoint_reuses_manifest_without_rendering_again():
    async def scenario():
        store, state, config = await ready_to_publish()
        store.crash_after_commit = True
        async def publish(request):
            return await publish_fixture(store, request)
        first = await make_publish_node(publish, store=store, limits=config,
                                       now=lambda: NOW)(state)
        assert first["status"] == "failed"
        assert first["final_manifest_ref"] is None
        async def forbidden(request):
            raise AssertionError("Committed report key must skip PDF rendering on replay")
        replay = await make_publish_node(forbidden, store=store, limits=config,
                                        now=lambda: NOW)(state)
        assert replay["status"] == "completed"
        assert replay["final_manifest_ref"] is not None
    asyncio.run(scenario())


def test_at14_stale_evaluation_never_invokes_publish_worker():
    async def scenario():
        store, state, config = await ready_to_publish()
        c = contracts()
        current = c.EvalVerdict.model_validate(await store.load(state["eval_ref"]))
        stale = current.model_copy(update={"report_hash": "e" * 64})
        state["eval_ref"] = await store.save(stale, key="stale-evaluation.json")
        async def forbidden(request):
            raise AssertionError("Stale pass cannot authorize PDF generation")
        result = await make_publish_node(forbidden, store=store, limits=config,
                                        now=lambda: NOW)(state)
        assert result["status"] == "failed"
        assert result["final_manifest_ref"] is None
    asyncio.run(scenario())


def test_at19_missing_committed_pdf_is_not_reused_as_completed():
    async def scenario():
        store, state, config = await ready_to_publish()
        async def publish(request):
            return await publish_fixture(store, request)
        first = await make_publish_node(publish, store=store, limits=config,
                                       now=lambda: NOW)(state)
        assert first["status"] == "completed"
        manifest = contracts().Manifest.model_validate(await store.load(first["final_manifest_ref"]))
        del store.payloads[manifest.report_pdf_ref.relative_path]
        async def forbidden(request):
            raise AssertionError("Corrupt completed artifact is never re-rendered silently")
        replay = await make_publish_node(forbidden, store=store, limits=config,
                                        now=lambda: NOW)(state)
        assert replay["status"] == "failed"
        assert replay["terminal_reason"] == "ARTIFACT_INTEGRITY_ERROR"
        assert replay["final_manifest_ref"] is None
    asyncio.run(scenario())
