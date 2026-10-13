"""Issue #26: real writer/PDF publication, durable exports and recovery.

Research and Judge decisions are deterministic fixtures. The writer, renderer,
artifact store, SQLite checkpointer and unchanged Supervisor are real services;
these tests do not claim a live model or retrieval evaluation.
"""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re

from langgraph.errors import NodeCancelledError
from pypdf import PdfReader
import pytest

from policy_fixtures import NOW, Resolver, assessor, evaluation, full_fixture, new_run
from main.agents.report.neutral_builder import NeutralWriter
from main.agents.report.neutral_publish import NeutralPublisher
from main.agents.report.neutral_renderers import neutral_markdown
from main.agents.report.renderers import _FONT_CANDIDATES
from main.agents.supervisor import contracts as c
from main.runtime.neutral.services import ServiceBundle, assemble_graph
from main.storage.artifacts import ArtifactIntegrityError, FileArtifactStore
from main.storage.checkpoints import open_checkpointer


EXPORTS = {"report_md": "report.md", "report_json": "report.json",
           "evaluation_json": "evaluation.json"}
PUBLIC_FILES = (*EXPORTS.values(), "report.pdf", "manifest.json")


def _publisher(store, resolver):
    font = os.environ.get("REPORT_FONT_PATH") or next(
        (path for path in _FONT_CANDIDATES if Path(path).is_file()),
        "/missing/korean-font.ttf")
    return NeutralPublisher(store=store, evidence_resolver=resolver,
                            artifact_root=store.root, font_path=font)


async def _prepared(tmp_path):
    memory, state = await new_run({"run_deadline_seconds": 86400})
    state["started_at"] = datetime.now(timezone.utc)
    state["deadline_at"] = state["started_at"] + timedelta(seconds=86400)
    _, verdicts, evidence, _, _ = await full_fixture(memory, state)
    store = FileArtifactStore(tmp_path / state["run_id"], run_id=state["run_id"])
    for key, payload in memory.payloads.items():
        await store.save(payload, key=key)
    config = c.RunConfig.model_validate(await store.load(state["config_ref"]))
    calls = {"writer": 0, "evaluator": 0, "publish": 0}
    resolver = Resolver(evidence)
    observed = {}

    async def unused(request):
        raise AssertionError("Approved research must not be called again")

    async def writer(request):
        calls["writer"] += 1
        report = await NeutralWriter()(request)
        observed["report"] = report.model_dump(mode="json")
        return report

    async def judge(request):
        calls["evaluator"] += 1
        verdict = evaluation(request.report)
        observed["evaluation"] = verdict.model_dump(mode="json")
        return verdict

    actual_publisher = _publisher(store, resolver)

    async def publish(request):
        calls["publish"] += 1
        return await actual_publisher(request)

    services = ServiceBundle(workers={"eligibility": unused,
        **{p.value: unused for p in c.PERSPECTIVES}, "writer": writer, "evaluator": judge},
        evidence_resolver=resolver, assess_evidence=await assessor(verdicts),
        publish_worker=publish, mock=True)
    return store, state, config, services, calls, observed


async def _invoke(tmp_path, store, state, config, services, *, resume=False):
    invocation = {"configurable": {"thread_id": state["thread_id"]}}
    async with open_checkpointer(tmp_path / "checkpoints.sqlite") as saver:
        graph = assemble_graph(bundle=services, store=store, run_config=config,
            checkpointer=saver, deadline_at=state["deadline_at"], now=lambda: NOW)
        return await graph.ainvoke(None if resume else state, invocation)


def test_real_publication_exports_evaluated_body_citations_and_exact_json(tmp_path):
    async def scenario():
        store, state, config, services, calls, observed = await _prepared(tmp_path)
        assert await store.approved_artifact_paths(state["run_id"], 1) is None
        result = await _invoke(tmp_path, store, state, config, services)
        assert result["status"] == "completed", result["last_error"]
        assert calls == {"writer": 1, "evaluator": 1, "publish": 1}
        reopened = FileArtifactStore(store.root, state["run_id"])
        paths = await reopened.approved_artifact_paths(state["run_id"], 1)
        assert set(paths) == set(PUBLIC_FILES)
        assert all(path.is_file() for path in paths.values())
        manifest = c.Manifest.model_validate(await reopened.load(result["final_manifest_ref"]))
        report = c.DraftReport.model_validate(json.loads(paths["report.json"].read_bytes()))
        verdict = c.EvalVerdict.model_validate(json.loads(paths["evaluation.json"].read_bytes()))
        assert report.model_dump(mode="json") == observed["report"]
        assert verdict.model_dump(mode="json") == observed["evaluation"]
        assert (manifest.run_id, manifest.report_revision, manifest.report_hash,
                manifest.evidence_epoch) == (state["run_id"], report.revision,
                                            report.content_hash, report.evidence_epoch)
        assert (verdict.report_revision, verdict.report_hash, verdict.evidence_epoch) == (
            report.revision, report.content_hash, report.evidence_epoch)
        markdown = paths["report.md"].read_text()
        assert markdown == neutral_markdown(report)
        assert markdown.startswith("## SUMMARY\n") and "## REFERENCE\n" in markdown
        assert "Confirmed" in markdown and report.citations
        for citation in report.citations:
            marker = f"[{citation.source_id} | {citation.evidence_id}]"
            escaped = re.sub(r"([\\`*_\[\]{}()#+.!|\-])", r"\\\1", marker)
            assert escaped in markdown
        for key, filename in EXPORTS.items():
            ref = manifest.artifacts[key]
            assert ref.relative_path == filename
            assert hashlib.sha256(paths[filename].read_bytes()).hexdigest() == ref.sha256
            await reopened.verify(ref)
        assert paths["report.json"].read_bytes() == c.canonical_json(report).encode()
        assert paths["evaluation.json"].read_bytes() == c.canonical_json(verdict).encode()
        pdf = PdfReader(paths["report.pdf"])
        text = "\n".join(page.extract_text() for page in pdf.pages)
        assert 1 <= len(pdf.pages) <= 10 and "기업 및 평가 범위" in text
        assert "REFERENCE" in text and "Confirmed" in text
        async with open_checkpointer(tmp_path / "checkpoints.sqlite") as saver:
            checkpoint = await saver.aget_tuple({"configurable": {"thread_id": state["thread_id"]}})
            assert checkpoint.checkpoint["channel_values"]["status"] == "completed"
    asyncio.run(scenario())


@pytest.mark.parametrize("filename", EXPORTS.values())
@pytest.mark.parametrize("failure", ["write_error", "changed_bytes"])
def test_export_save_or_verification_failure_never_completes(tmp_path, filename, failure):
    async def scenario():
        store, state, config, services, _, _ = await _prepared(tmp_path)
        original_write = store._write
        injected = False

        def fail_export(key, data, media_type="application/json"):
            nonlocal injected
            if key == filename and failure == "write_error":
                injected = True
                raise OSError("Injected export disk failure")
            ref = original_write(key, data, media_type)
            if key == filename and failure == "changed_bytes":
                injected = True
                (store.root / key).write_bytes(b"Modified after saving the export")
            return ref

        store._write = fail_export
        result = await _invoke(tmp_path, store, state, config, services)
        assert injected
        assert result["status"] == "failed"
        assert result["terminal_reason"] == "ARTIFACT_INTEGRITY_ERROR"
        assert result["final_manifest_ref"] is None
        assert not (store.root / "manifest.json").exists()
        reopened = FileArtifactStore(store.root, state["run_id"])
        assert await reopened.approved_artifact_paths(state["run_id"], 1) is None
        assert await reopened.approved_report_path(state["run_id"], 1) is None
    asyncio.run(scenario())


@pytest.mark.parametrize("crash_at", ["before_manifest", "after_manifest"])
def test_publication_crash_reopens_and_resumes_the_same_evaluated_exports(tmp_path, crash_at):
    async def scenario():
        store, state, config, services, calls, observed = await _prepared(tmp_path)
        original_write = store._write
        interrupted = False

        def crash(key, data, media_type="application/json"):
            nonlocal interrupted
            if key == "manifest.json" and crash_at == "before_manifest":
                interrupted = True
                raise asyncio.CancelledError()
            ref = original_write(key, data, media_type)
            if key == "manifest.json" and crash_at == "after_manifest":
                interrupted = True
                raise asyncio.CancelledError()
            return ref

        store._write = crash
        with pytest.raises((asyncio.CancelledError, NodeCancelledError)):
            await _invoke(tmp_path, store, state, config, services)
        assert interrupted
        assert all((store.root / filename).is_file() for filename in EXPORTS.values())
        before = {filename: (store.root / filename).read_bytes() for filename in EXPORTS.values()}
        reopened = FileArtifactStore(store.root, state["run_id"])
        published = await reopened.approved_artifact_paths(state["run_id"], 1)
        assert (published is None) == (crash_at == "before_manifest")
        actual_publisher = _publisher(reopened, services.evidence_resolver)

        async def publish(request):
            calls["publish"] += 1
            return await actual_publisher(request)

        resumed_services = replace(services, publish_worker=publish)
        result = await _invoke(tmp_path, reopened, state, config, resumed_services, resume=True)
        assert result["status"] == "completed", result["last_error"]
        assert calls == {"writer": 1, "evaluator": 1,
                         "publish": 2 if crash_at == "before_manifest" else 1}
        paths = await reopened.approved_artifact_paths(state["run_id"], 1)
        assert {filename: paths[filename].read_bytes() for filename in EXPORTS.values()} == before
        assert json.loads(paths["report.json"].read_bytes()) == observed["report"]
        assert json.loads(paths["evaluation.json"].read_bytes()) == observed["evaluation"]
    asyncio.run(scenario())


@pytest.mark.parametrize("filename", PUBLIC_FILES)
@pytest.mark.parametrize("damage", ["changed", "missing"])
def test_completed_publication_rejects_each_changed_or_missing_file(tmp_path, filename, damage):
    async def scenario():
        store, state, config, services, _, _ = await _prepared(tmp_path)
        result = await _invoke(tmp_path, store, state, config, services)
        assert result["status"] == "completed", result["last_error"]
        paths = await store.approved_artifact_paths(state["run_id"], 1)
        if damage == "missing":
            paths[filename].unlink()
        else:
            paths[filename].write_bytes(b"Modified committed artifact")
        reopened = FileArtifactStore(store.root, state["run_id"])
        # A removed manifest marker hides publication altogether. A remaining
        # marker must fail closed when any referenced content is damaged.
        if filename == "manifest.json" and damage == "missing":
            assert await reopened.approved_artifact_paths(state["run_id"], 1) is None
            assert await reopened.approved_report_path(state["run_id"], 1) is None
        else:
            with pytest.raises(ArtifactIntegrityError):
                await reopened.approved_artifact_paths(state["run_id"], 1)
            with pytest.raises(ArtifactIntegrityError):
                await reopened.approved_report_path(state["run_id"], 1)
    asyncio.run(scenario())
