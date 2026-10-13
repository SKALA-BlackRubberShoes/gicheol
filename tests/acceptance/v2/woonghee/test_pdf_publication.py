import asyncio
import hashlib
from pathlib import Path
import runpy

import pytest
from pypdf import PdfReader, PdfWriter

from main.agents.supervisor import contracts as c
from main.agents.report.neutral_builder import build_neutral_report
from main.agents.report.neutral_publish import NeutralPublisher
from main.agents.report.neutral_renderers import render_neutral_pdf, validate_pdf, PdfLayoutError, report_document
from main.agents.report.renderers import render_pdf

case = runpy.run_path(str(Path(__file__).parents[3] / "fixtures/v2/woonghee/report_case.py"))
writer_request, publish_request = case["writer_request"], case["publish_request"]
FONT = case["FONT"]


def publisher(tmp_path, request, font=FONT):
    store = case["Store"](tmp_path)
    worker = NeutralPublisher(store=store, evidence_resolver=case["Resolver"](request),
                              artifact_root=tmp_path, font_path=font)
    return worker, store


def test_at12_actual_korean_pdf_and_staging_only(tmp_path):
    request = writer_request()
    report = build_neutral_report(request)
    before = report.model_dump(mode="json")
    worker, store = publisher(tmp_path, request)
    result = asyncio.run(worker(publish_request(request, report)))
    assert result.pdf_validation_pass and 1 <= result.page_count <= 10
    assert 0 < result.summary_page_fraction <= 0.5
    assert result.final_manifest_ref is None
    path = tmp_path / result.staging_pdf_ref.relative_path
    assert hashlib.sha256(path.read_bytes()).hexdigest() == result.staging_pdf_ref.sha256
    reader = PdfReader(path)
    text = "\n".join(p.extract_text() for p in reader.pages)
    assert "로봇" in text and "기업 및 평가 범위" in text and "REFERENCE" in text
    assert reader.metadata.author == "Neutral Startup Evaluation"
    assert not (tmp_path / "report.pdf").exists() and not (tmp_path / "manifest.json").exists()
    assert report.model_dump(mode="json") == before
    assert store.saved == {}
    for section in report.sections[:-1]:
        for paragraph in section.paragraphs:
            if paragraph.evidence_ids:
                assert any(cit.paragraph_id == paragraph.paragraph_id for cit in report.citations)


@pytest.mark.parametrize("change", ["revision", "epoch", "hash", "content", "research", "eligibility", "gate", "rubric", "eval_fail"])
def test_at14_and_at17_reject_stale_approval_before_render(tmp_path, change):
    request = writer_request()
    report = build_neutral_report(request)
    payload = publish_request(request, report)
    if change == "revision":
        payload.evaluation.report_revision += 1
    elif change == "epoch":
        payload.evaluation.evidence_epoch += 1
    elif change == "hash":
        payload.evaluation.report_hash = "f" * 64
    elif change == "content":
        payload.report.sections[0].paragraphs[0].text += "추가 내용"
    elif change == "research":
        payload.perspective_results[c.PERSPECTIVES[0]].result_hash = "f" * 64
    elif change == "eligibility":
        payload.eligibility.verdict = "unknown"
        payload.eligibility.unlisted = "unknown"
    elif change == "gate":
        payload.gate.passed = False
    elif change == "rubric":
        payload.evaluation.rubric_version = "old"
    else:
        payload.evaluation.overall_pass = False
    worker, _ = publisher(tmp_path, request)
    with pytest.raises(ValueError):
        asyncio.run(worker(payload))
    assert not (tmp_path / "staging").exists()


def test_new_content_hash_with_old_eval_is_rejected():
    request = writer_request()
    report = build_neutral_report(request)
    payload = publish_request(request, report).model_dump(mode="json")
    payload["report"]["sections"][0]["paragraphs"][0]["text"] += " 변경된 문장"
    payload["report"]["content_hash"] = None
    with pytest.raises(ValueError, match="latest passing"):
        c.PublishRequest.model_validate(payload)


def test_more_than_five_pages_is_allowed_only_in_neutral_path(tmp_path):
    report = build_neutral_report(writer_request(long_text=240))
    metrics = render_neutral_pdf(report, tmp_path / "neutral.pdf", font_path=FONT)
    assert 5 < metrics["page_count"] <= 10
    with pytest.raises(ValueError, match="1 to 5"):
        render_pdf(report_document(report), tmp_path / "legacy.pdf", font_path=FONT, max_pages=10)


def test_over_ten_pages_preserves_draft_and_diagnostics(tmp_path):
    request = writer_request(long_text=700)
    report = build_neutral_report(request)
    digest = report.content_hash
    worker, store = publisher(tmp_path, request)
    with pytest.raises(PdfLayoutError, match="10-page"):
        asyncio.run(worker(publish_request(request, report)))
    assert report.content_hash == digest
    assert store.saved["diagnostics/pdf-1.json"]["code"] == "PDF_LAYOUT_FAILED"
    assert not list((tmp_path / "staging").rglob("*.pdf"))
    assert not (tmp_path / "report.pdf").exists()


def test_half_page_summary_failure_does_not_overwrite_existing_file(tmp_path):
    report = build_neutral_report(writer_request())
    report.sections[0].paragraphs[0].text *= 200
    report.content_hash = None
    report = c.DraftReport.model_validate(report.model_dump(mode="json"))
    path = tmp_path / "existing.pdf"
    path.write_bytes(b"previous valid staging")
    with pytest.raises(PdfLayoutError, match="SUMMARY exceeds half"):
        render_neutral_pdf(report, path, font_path=FONT)
    assert path.read_bytes() == b"previous valid staging"


@pytest.mark.parametrize("font", ["", "/missing/font.ttf"])
def test_missing_font_fails_with_diagnostics(tmp_path, font):
    request = writer_request()
    report = build_neutral_report(request)
    worker, store = publisher(tmp_path, request, font)
    with pytest.raises(PdfLayoutError):
        asyncio.run(worker(publish_request(request, report)))
    assert store.saved and not list((tmp_path / "staging").rglob("*.pdf"))


def test_reference_mismatch_blocks_staging(tmp_path):
    request = writer_request()
    report = build_neutral_report(request)
    report.sections[-1].paragraphs.pop()
    report.content_hash = None
    report = c.DraftReport.model_validate(report.model_dump(mode="json"))
    worker, _ = publisher(tmp_path, request)
    with pytest.raises(ValueError, match="REFERENCE"):
        asyncio.run(worker(publish_request(request, report)))
    assert not (tmp_path / "staging").exists()


def test_rendered_pdf_content_mismatch_is_rejected(tmp_path):
    report = build_neutral_report(writer_request())
    path = tmp_path / "report.pdf"
    metrics = render_neutral_pdf(report, path, font_path=FONT)
    report.sections[0].paragraphs[0].text += " 승인되지 않은 문장"
    report.content_hash = None
    report = c.DraftReport.model_validate(report.model_dump(mode="json"))
    with pytest.raises(PdfLayoutError, match="PDF text or citations"):
        validate_pdf(path, report, summary_page_fraction=metrics["summary_page_fraction"])


def test_non_a4_candidate_is_rejected(tmp_path):
    report = build_neutral_report(writer_request())
    path = tmp_path / "report.pdf"
    metrics = render_neutral_pdf(report, path, font_path=FONT)
    reader, writer = PdfReader(path), PdfWriter()
    for page in reader.pages:
        page.mediabox.upper_right = (612, 792)
        writer.add_page(page)
    writer.write(tmp_path / "letter.pdf")
    with pytest.raises(PdfLayoutError, match="A4"):
        validate_pdf(tmp_path / "letter.pdf", report, summary_page_fraction=metrics["summary_page_fraction"])


def test_unembedded_font_is_rejected(tmp_path):
    report = build_neutral_report(writer_request())
    path = tmp_path / "report.pdf"
    metrics = render_neutral_pdf(report, path, font_path=FONT)
    reader, writer = PdfReader(path), PdfWriter()
    for page in reader.pages:
        for font_ref in page["/Resources"]["/Font"].values():
            descriptor = font_ref.get_object().get("/FontDescriptor")
            if descriptor:
                descriptor.get_object().pop("/FontFile2", None)
        writer.add_page(page)
    writer.write(tmp_path / "unembedded.pdf")
    with pytest.raises(PdfLayoutError, match="embedded"):
        validate_pdf(tmp_path / "unembedded.pdf", report, summary_page_fraction=metrics["summary_page_fraction"])


def test_symlink_staging_is_rejected(tmp_path):
    request = writer_request()
    worker, _ = publisher(tmp_path, request)
    external = tmp_path.parent / (tmp_path.name + "-external")
    external.mkdir()
    (tmp_path / "staging").symlink_to(external)
    with pytest.raises(ValueError, match="escapes"):
        asyncio.run(worker(publish_request(request, build_neutral_report(request))))
    assert not list(external.iterdir())


def test_source_integrity_failure_does_not_render(tmp_path):
    request = writer_request()
    worker, _ = publisher(tmp_path, request)
    (tmp_path / "sources/original.txt").write_text("tampered")
    with pytest.raises(ValueError, match="integrity"):
        asyncio.run(worker(publish_request(request, build_neutral_report(request))))
    assert not (tmp_path / "staging").exists()


def test_actual_writer_and_pdf_worker_connect_to_existing_supervisor_graph(tmp_path):
    """Actual writer/PDF, injected deterministic verifier/Judge/store contracts."""
    from langgraph.checkpoint.memory import InMemorySaver
    from main.agents.report.neutral_builder import NeutralWriter
    from main.agents.supervisor.agent import Supervisor
    from main.graph.supervisor_workflow import build_supervisor_graph

    shared = runpy.run_path(str(Path(__file__).parents[3] / "unit/v2/dongwook/policy_fixtures.py"))

    class GraphStore(shared["MemoryStore"]):
        def __init__(self):
            super().__init__()
            self.manifests = {}

        async def verify(self, ref):
            if ref.relative_path.startswith("staging/"):
                assert hashlib.sha256((tmp_path / ref.relative_path).read_bytes()).hexdigest() == ref.sha256
            else:
                await super().verify(ref)

        async def find_manifest(self, run_id, report_revision):
            return self.manifests.get((run_id, report_revision))

        async def commit_manifest(self, manifest):
            await self.verify(manifest.report_pdf_ref)
            ref = await super().commit_manifest(manifest)
            self.manifests[manifest.run_id, manifest.report_revision] = ref
            return ref

    async def scenario():
        original, state = await shared["new_run"]()
        store = GraphStore()
        store.payloads.update(original.payloads)
        _, verdicts, bundle, _, _ = await shared["full_fixture"](store, state)
        resolver = shared["Resolver"](bundle)
        config = c.RunConfig.model_validate(await store.load(state["config_ref"]))
        supervisor = Supervisor(store, resolver, await shared["assessor"](verdicts), now=lambda: shared["NOW"])
        async def unused(request):
            raise AssertionError("approved research must not be called again")
        async def judge(request):
            return shared["evaluation"](request.report)
        workers = {node: unused for node in ("eligibility", *(p.value for p in c.PERSPECTIVES))}
        workers.update(writer=NeutralWriter(), evaluator=judge)
        graph = build_supervisor_graph(supervisor=supervisor, workers=workers,
            publish_worker=NeutralPublisher(store=store, evidence_resolver=resolver,
                artifact_root=tmp_path, font_path=FONT), store=store, run_config=config,
            checkpointer=InMemorySaver(), now=lambda: shared["NOW"])
        result = await graph.ainvoke(state, {"configurable": {"thread_id": state["thread_id"]}})
        assert result["status"] == "completed", result["last_error"]
        manifest = c.Manifest.model_validate(await store.load(result["final_manifest_ref"]))
        assert manifest.report_revision == result["report_revision"]
        assert (tmp_path / manifest.report_pdf_ref.relative_path).is_file()
        assert PdfReader(tmp_path / manifest.report_pdf_ref.relative_path).pages
        assert len(store.manifests) == 1
    asyncio.run(scenario())
