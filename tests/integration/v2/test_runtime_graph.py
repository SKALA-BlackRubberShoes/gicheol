"""Real filesystem + SQLite + unchanged Supervisor; business services are MOCK.

The blank PDF is a storage fixture, never AT12 layout or AT13 live evidence.
"""
import asyncio
from io import BytesIO
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from pypdf import PdfWriter
from langgraph.errors import NodeCancelledError
from policy_fixtures import (NOW, Resolver, assessor, contracts, draft, evaluation,
                             full_fixture, new_run)

from main.graph.supervisor_state import SupervisorState, validate_state
from main.neutral_eval import _execute_graph
from main.runtime.neutral.services import ServiceBundle, assemble_graph
from main.storage.artifacts import FileArtifactStore
from main.storage.checkpoints import open_checkpointer


async def prepared(tmp_path):
    c = contracts()
    memory, state = await new_run({"run_deadline_seconds": 86400})
    # The inherited business evidence fixture is dated; the run budget must
    # still be fresh when this regression suite runs after its authoring day.
    state["started_at"] = datetime.now(timezone.utc)
    state["deadline_at"] = state["started_at"] + timedelta(seconds=86400)
    _, verdicts, bundle, _, _ = await full_fixture(memory, state)
    store = FileArtifactStore(tmp_path / state["run_id"], run_id=state["run_id"])
    for key, payload in memory.payloads.items():
        await store.save(payload, key=key)
    config = c.RunConfig.model_validate(await store.load(state["config_ref"]))
    calls = {"writer": 0, "evaluator": 0, "publish": 0}
    async def unused(request):
        raise AssertionError("Already sufficient view must not be recalled")
    async def writer(request):
        calls["writer"] += 1
        return c.DraftReport(revision=request.report_revision, evidence_epoch=request.evidence_epoch,
            sections=[dict(section_id=s, title=s, paragraphs=[]) for s in c.SECTION_ORDER],
            claim_ids=[], citations=[])
    async def judge(request):
        calls["evaluator"] += 1
        return evaluation(request.report)
    async def publish(request):
        calls["publish"] += 1
        pdf = PdfWriter()
        pdf.add_blank_page(width=595, height=842)
        output = BytesIO()
        pdf.write(output)
        ref = await store.save_bytes(output.getvalue(), key=f"staging/report-{request.report.revision}.pdf",
                                     media_type="application/pdf")
        return c.PublishResult(report_revision=request.report.revision,
            report_hash=request.report.content_hash, evidence_epoch=request.report.evidence_epoch,
            staging_pdf_ref=ref, pdf_validation_pass=True, page_count=1, summary_page_fraction=0)
    services = ServiceBundle(workers={"eligibility": unused, **{p.value: unused for p in c.PERSPECTIVES},
                                     "writer": writer, "evaluator": judge},
        evidence_resolver=Resolver(bundle), assess_evidence=await assessor(verdicts),
        publish_worker=publish, mock=True)
    return store, state, config, services, calls


def test_actual_sqlite_graph_completes_and_reopens_verified_manifest(tmp_path):
    async def scenario():
        store, state, config, bundle, calls = await prepared(tmp_path)
        async with open_checkpointer(tmp_path / "checkpoints.sqlite") as saver:
            graph = assemble_graph(bundle=bundle, store=store, run_config=config,
                checkpointer=saver, deadline_at=state["deadline_at"], now=lambda: NOW)
            result = await graph.ainvoke(state, {"configurable": {"thread_id": state["thread_id"]}})
        assert result["status"] == "completed"
        assert calls == {"writer": 1, "evaluator": 1, "publish": 1}
        reopened = FileArtifactStore(store.root, run_id=state["run_id"])
        assert await reopened.find_manifest(state["run_id"], 1) == result["final_manifest_ref"]
        assert (await reopened.approved_report_path(state["run_id"], 1)).is_file()
        async with open_checkpointer(tmp_path / "checkpoints.sqlite") as saver:
            checkpoint = await saver.aget_tuple({"configurable": {"thread_id": state["thread_id"]}})
            assert checkpoint.checkpoint["channel_values"]["status"] == "completed"
    asyncio.run(scenario())


def test_at09_sqlite_replay_after_result_receipt_before_checkpoint(tmp_path):
    async def scenario():
        store, state, config, bundle, calls = await prepared(tmp_path)
        save_receipt = store.save_receipt
        crashed = False
        async def crash(task, result):
            nonlocal crashed
            await save_receipt(task, result)
            if not crashed:
                crashed = True
                raise asyncio.CancelledError()
        store.save_receipt = crash
        invocation = {"configurable": {"thread_id": state["thread_id"]}}
        async with open_checkpointer(tmp_path / "checkpoints.sqlite") as saver:
            graph = assemble_graph(bundle=bundle, store=store, run_config=config,
                checkpointer=saver, deadline_at=state["deadline_at"], now=lambda: NOW)
            try:
                await graph.ainvoke(state, invocation)
            except (asyncio.CancelledError, NodeCancelledError):
                pass
            else:
                raise AssertionError("Crash injection must interrupt execution")
        store.save_receipt = save_receipt
        async with open_checkpointer(tmp_path / "checkpoints.sqlite") as saver:
            graph = assemble_graph(bundle=bundle, store=store, run_config=config,
                checkpointer=saver, deadline_at=state["deadline_at"], now=lambda: NOW)
            result = await graph.ainvoke(None, invocation)
        assert result["status"] == "completed"
        assert calls == {"writer": 1, "evaluator": 1, "publish": 1}
        assert len(result["accepted_task_ids"]) == 2
    asyncio.run(scenario())


def test_runner_writes_trace_metadata_and_decisions(tmp_path):
    async def scenario():
        store, state, config, bundle, calls = await prepared(tmp_path)
        runtime = SimpleNamespace(run_config=config, langsmith_tracing=False)
        async with open_checkpointer(tmp_path / "checkpoints.sqlite") as saver:
            result = await _execute_graph(state=state, runtime=runtime, store=store,
                                          bundle=bundle, checkpointer=saver)
        assert result["status"] == "completed"
        assert (store.root / "decisions.jsonl").is_file()
        assert '"mock":true' in (store.root / "trace.jsonl").read_text()
    asyncio.run(scenario())
