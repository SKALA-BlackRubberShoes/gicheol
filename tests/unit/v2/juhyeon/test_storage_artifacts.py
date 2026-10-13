import asyncio
from datetime import datetime, timezone
from io import BytesIO
import json

from pypdf import PdfWriter
import pytest

from main.agents.supervisor import contracts as c
from main.storage.artifacts import ArtifactIntegrityError, FileArtifactStore
from main.storage.receipts import ReceiptStore


def run(awaitable):
    return asyncio.run(awaitable)


async def make_manifest(store, *, revision=1):
    report = c.DraftReport(revision=revision, evidence_epoch=4,
        sections=[dict(section_id=s, title=s.value, paragraphs=[]) for s in c.SECTION_ORDER],
        claim_ids=[], citations=[])
    evaluation = c.EvalVerdict(report_revision=revision, report_hash=report.content_hash,
        evidence_epoch=4, structural_checks={"citations": True},
        criterion_scores={criterion: 3 for criterion in c.Criterion}, findings=[],
        overall_pass=True, repair_kind="none", rubric_version="1.0",
        judge_model="fixture-judge", prompt_hash="a" * 64)
    reason = await store.save({"passed": True}, key="gate-reason.json")
    gate = c.GateCertificate(evidence_epoch=4, eligibility_hash="b" * 64,
        perspective_hashes={p: c.canonical_hash(p) for p in c.PERSPECTIVES},
        rubric_version="1.0", passed=True, reason_ref=reason)
    pdf = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    writer.write(pdf)
    return c.Manifest(run_id="run-1", status="completed", report_revision=revision,
        report_hash=report.content_hash, evidence_epoch=4, eligibility_hash=gate.eligibility_hash,
        perspective_hashes=gate.perspective_hashes,
        gate_ref=await store.save(gate, key="gate.json"),
        draft_ref=await store.save(report, key="draft.json"),
        evaluation_ref=await store.save(evaluation, key="evaluation.json"),
        report_pdf_ref=await store.save_bytes(pdf.getvalue(), key=f"staging/report-{revision}.pdf", media_type="application/pdf"),
        pdf_validation_pass=True, trace_id="trace-1", versions={"prompt": "a" * 64},
        committed_at=datetime.now(timezone.utc))


async def make_receipt(store):
    payload = {"task_id": "task-1", "question": "test"}
    task = c.TaskEnvelope(task_id="task-1", node="eligibility", logical_attempt=1,
        evidence_epoch=0, payload_ref=await store.save(payload, key="input.json"),
        input_hash=c.canonical_hash(payload), dispatched_at=datetime.now(timezone.utc))
    result = c.ResultEnvelope(task_id=task.task_id, outcome="success",
        result_ref=await store.save({"task_id": task.task_id, "answer": True}, key="result.json"))
    return task, result


def test_round_trip_uses_actual_canonical_bytes_and_binary_verify(tmp_path):
    async def scenario():
        store = FileArtifactStore(tmp_path)
        payload = {"한글": "근거", "number": 3}
        ref = await store.save(payload, key="a/data.json")
        assert (tmp_path / ref.relative_path).read_bytes() == c.canonical_json(payload).encode()
        assert ref.sha256 == c.canonical_hash(payload)
        assert await store.load(ref) == payload
        binary = await store.save_bytes(b"\x00\xff", key="original.pdf", media_type="application/pdf")
        await store.verify(binary)
        with pytest.raises(ArtifactIntegrityError, match="JSON"):
            await store.load(binary)
        (tmp_path / ref.relative_path).write_text('{"changed":true}')
        with pytest.raises(ArtifactIntegrityError):
            await store.load(ref)
    run(scenario())


@pytest.mark.parametrize("key", ["../escape.json", "/absolute.json", "a/../x", "a//x", "a\\b", "a:b"])
def test_unsafe_paths_never_write(tmp_path, key):
    with pytest.raises(ValueError):
        run(FileArtifactStore(tmp_path).save({}, key=key))


@pytest.mark.parametrize("kind", ["directory", "file", "dangling"])
def test_symlinks_rejected_for_reads_and_writes(tmp_path, kind):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "payload.json").write_text("{}")
    store = FileArtifactStore(root)
    if kind == "directory":
        (root / "link").symlink_to(outside, target_is_directory=True)
        key = "link/payload.json"
    else:
        (root / "link.json").symlink_to(outside / ("payload.json" if kind == "file" else "missing.json"))
        key = "link.json"
    ref = c.ArtifactRef(relative_path=key, sha256=c.canonical_hash({}), media_type="application/json")
    with pytest.raises(ArtifactIntegrityError):
        run(store.verify(ref))
    with pytest.raises(ArtifactIntegrityError):
        run(store.save({"bad": True}, key=key))
    assert (outside / "payload.json").read_text() == "{}"


def test_failed_atomic_replace_preserves_previous_artifact(tmp_path, monkeypatch):
    async def scenario():
        store = FileArtifactStore(tmp_path)
        original = await store.save({"version": 1}, key="data.json")
        def fail(*args, **kwargs):
            raise OSError("simulated disk error before rename")
        monkeypatch.setattr("main.storage.artifacts.os.replace", fail)
        with pytest.raises(ArtifactIntegrityError):
            await store.save({"version": 2}, key="data.json")
        assert await store.load(original) == {"version": 1}
        assert not list(tmp_path.glob(".*.tmp"))
    run(scenario())


def test_original_snapshots_are_immutable_but_diagnostics_can_update(tmp_path):
    async def scenario():
        store = FileArtifactStore(tmp_path)
        original = await store.save({"model": "original"}, key="snapshots/config.json")
        assert await store.save({"model": "original"}, key="snapshots/config.json") == original
        with pytest.raises(ArtifactIntegrityError):
            await store.save({"model": "changed"}, key="snapshots/config.json")
        assert await store.load(original) == {"model": "original"}
        await store.save({"status": "interrupted"}, key="diagnostics.json")
        latest = await store.save({"status": "failed"}, key="diagnostics.json")
        assert await store.load(latest) == {"status": "failed"}
    run(scenario())


def test_receipt_reopen_same_input_and_changed_input_rejected(tmp_path):
    async def scenario():
        store = FileArtifactStore(tmp_path)
        task, result = await make_receipt(store)
        await store.save_receipt(task, result)
        reopened = FileArtifactStore(tmp_path)
        assert await reopened.find_receipt(task.task_id, task.input_hash) == result
        await reopened.save_receipt(task, result)
        with pytest.raises(ArtifactIntegrityError, match="changed inputs"):
            await reopened.find_receipt(task.task_id, "f" * 64)
        (tmp_path / result.result_ref.relative_path).write_text('{"answer":false}')
        with pytest.raises(ArtifactIntegrityError):
            await reopened.find_receipt(task.task_id, task.input_hash)
    run(scenario())


def test_receipt_tamper_and_input_tamper_rejected(tmp_path):
    async def scenario():
        store = FileArtifactStore(tmp_path)
        task, result = await make_receipt(store)
        await store.save_receipt(task, result)
        key = ReceiptStore.key(task.task_id)
        raw = json.loads((tmp_path / key).read_text())
        raw["result"]["task_id"] = "other"
        (tmp_path / key).write_text(json.dumps(raw))
        with pytest.raises(ArtifactIntegrityError):
            await store.find_receipt(task.task_id, task.input_hash)
        (tmp_path / task.payload_ref.relative_path).write_text('{"question":"changed"}')
        with pytest.raises(ArtifactIntegrityError):
            await store.save_receipt(task, result)
    run(scenario())


def test_receipt_revalidates_nested_artifact_references(tmp_path):
    async def scenario():
        store = FileArtifactStore(tmp_path)
        task, result = await make_receipt(store)
        document = await store.save_bytes(b"approved report text", key="draft.md", media_type="text/markdown")
        result.result_ref = await store.save({"task_id": task.task_id,
            "document_ref": document.model_dump(mode="json")}, key="result.json")
        await store.save_receipt(task, result)
        (tmp_path / "draft.md").unlink()
        with pytest.raises(ArtifactIntegrityError):
            await store.find_receipt(task.task_id, task.input_hash)
    run(scenario())


def test_transient_receipt_can_resolve_but_final_receipt_is_immutable(tmp_path):
    async def scenario():
        store = FileArtifactStore(tmp_path)
        task, result = await make_receipt(store)
        transient = c.ResultEnvelope(task_id=task.task_id, outcome="transient_error",
            error=c.ErrorSummary(code="MODEL_OUTPUT_INVALID", node="eligibility", retriable=True, message="timeout"))
        await store.save_receipt(task, transient)
        await store.save_receipt(task, result)
        with pytest.raises(ArtifactIntegrityError, match="cannot be replaced"):
            await store.save_receipt(task, transient)
    run(scenario())


def test_manifest_last_reopen_and_idempotent_commit(tmp_path):
    async def scenario():
        store = FileArtifactStore(tmp_path, "run-1")
        manifest = await make_manifest(store)
        assert await store.find_manifest("run-1", 1) is None
        assert await store.approved_report_path("run-1", 1) is None
        ref = await store.commit_manifest(manifest)
        assert ref.relative_path == "manifest.json"
        reopened = FileArtifactStore(tmp_path, "run-1")
        assert await reopened.find_manifest("run-1", 1) == ref
        assert await reopened.commit_manifest(manifest.model_copy(update={"committed_at": datetime.now(timezone.utc)})) == ref
        assert await reopened.approved_report_path("run-1", 1) == tmp_path / manifest.report_pdf_ref.relative_path
        assert await reopened.find_manifest("run-1", 2) is None
        assert await reopened.find_manifest("other-run", 1) is None
    run(scenario())


def test_manifest_revalidates_source_nested_inside_gate_reason_after_commit(tmp_path):
    async def scenario():
        store = FileArtifactStore(tmp_path, "run-1")
        manifest = await make_manifest(store)
        source_ref = await store.save_bytes(b"verified source excerpt", key="source.txt", media_type="text/plain")
        reason_ref = await store.save({"source": {"content_ref": source_ref.model_dump(mode="json")}}, key="reason-with-source.json")
        gate = c.GateCertificate.model_validate(await store.load(manifest.gate_ref))
        gate.reason_ref = reason_ref
        manifest.gate_ref = await store.save(gate, key="gate-with-source.json")
        await store.commit_manifest(manifest)
        assert await store.approved_report_path("run-1", 1) is not None
        (tmp_path / "source.txt").write_bytes(b"tampered source excerpt")
        with pytest.raises(ArtifactIntegrityError):
            await FileArtifactStore(tmp_path, "run-1").approved_report_path("run-1", 1)
    run(scenario())


@pytest.mark.parametrize("change", ["gate", "evaluation", "pdf", "reason", "artifact", "manifest"])
def test_manifest_refuses_stale_or_tampered_approval(tmp_path, change):
    async def scenario():
        store = FileArtifactStore(tmp_path, "run-1")
        manifest = await make_manifest(store)
        if change == "gate":
            raw = await store.load(manifest.gate_ref)
            raw["evidence_epoch"] += 1
            manifest.gate_ref = await store.save(raw, key="stale-gate.json")
        elif change == "evaluation":
            raw = await store.load(manifest.evaluation_ref)
            raw["report_hash"] = "c" * 64
            manifest.evaluation_ref = await store.save(raw, key="stale-evaluation.json")
        elif change == "pdf":
            manifest.report_pdf_ref = await store.save_bytes(b"%PDF-1.4 fake", key="staging/bad.pdf", media_type="application/pdf")
        elif change == "reason":
            (tmp_path / "gate-reason.json").write_text("{}")
        elif change == "artifact":
            missing = c.ArtifactRef(relative_path="missing.json", sha256="a" * 64, media_type="application/json")
            manifest.artifacts = {"missing": missing}
        else:
            await store.commit_manifest(manifest)
            raw = json.loads((tmp_path / "manifest.json").read_text())
            raw["trace_id"] = "tampered"
            (tmp_path / "manifest.json").write_text(json.dumps(raw))
            with pytest.raises(ArtifactIntegrityError):
                await store.find_manifest("run-1", 1)
            return
        with pytest.raises(ArtifactIntegrityError):
            await store.commit_manifest(manifest)
        assert not (tmp_path / "manifest.json").exists()
    run(scenario())
