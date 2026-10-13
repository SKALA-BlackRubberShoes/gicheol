import asyncio
from pathlib import Path
import runpy

import pytest

from main.agents.supervisor import contracts as c
from main.agents.report.neutral_builder import build_neutral_report, validate_neutral_report, NeutralWriter
from main.agents.report.neutral_renderers import neutral_markdown

case = runpy.run_path(str(Path(__file__).parents[3] / "fixtures/v2/woonghee/report_case.py"))
writer_request = case["writer_request"]


def test_writer_preserves_claims_citations_fixed_order_and_unknown_dates():
    request = writer_request()
    before = request.model_dump(mode="json")
    report = build_neutral_report(request)
    assert request.model_dump(mode="json") == before
    assert tuple(s.section_id for s in report.sections) == c.SECTION_ORDER
    assert set(report.claim_ids) == {cl.claim_id for r in request.perspective_results.values() for cl in r.claims}
    assert report.content_hash == c.canonical_hash(report.model_dump(mode="json", include={"sections", "claim_ids", "citations", "evidence_epoch"}))
    assert [p.text for p in report.sections[-1].paragraphs][0].find("발행일 미상") >= 0
    md = neutral_markdown(report)
    assert md.startswith("## SUMMARY\n")
    assert "## REFERENCE" in md
    assert r"\[source\-0" in md  # ID brackets are safely escaped as plain Markdown.
    assert "검증 기관 2" not in md  # Unused source is excluded from REFERENCE.


@pytest.mark.parametrize("change", ["gate", "support", "evidence", "company", "future", "question", "gap", "unverified"])
def test_writer_rejects_unapproved_inputs(change):
    request = writer_request()
    if change == "gate":
        request.gate.evidence_epoch += 1
    elif change == "support":
        request.claim_supports[0].claim_hash = "f" * 64
    elif change == "evidence":
        request.evidence[0].quote += "변경"
    elif change == "company":
        request.evidence[0].company_id = "other-company"
    elif change == "future":
        request.sources[0].published_at = request.as_of_date.replace(year=2027)
    elif change == "question":
        request.perspective_results[c.PERSPECTIVES[0]].answered_questions = {}
    elif change == "gap":
        request.perspective_results[c.PERSPECTIVES[0]].gaps.append(c.Gap(gap_id="g", perspective=c.PERSPECTIVES[0],
            kind="missing_evidence", severity="critical", question="미해결 질문"))
    else:
        request.claim_supports[0].support_status = "unverified"
    with pytest.raises(ValueError):
        build_neutral_report(request)


@pytest.mark.parametrize("change", ["new_fact", "new_claim", "wrong_evidence", "reference", "heading", "drop_limit"])
def test_model_edits_cannot_add_facts_or_drop_provenance(change):
    request = writer_request()
    report = build_neutral_report(request)
    if change == "new_fact":
        report.sections[0].paragraphs[0].text = "이 회사에 투자하는 것이 가장 좋다."
    elif change == "new_claim":
        report.sections[0].paragraphs[0].claim_ids = ["invented"]
    elif change == "wrong_evidence":
        report.sections[0].paragraphs[0].evidence_ids = [request.evidence[-1].evidence_id]
    elif change == "reference":
        report.sections[-1].paragraphs.pop()
    elif change == "heading":
        report.sections[1].title = "투자 순위"
    else:
        for section in report.sections:
            section.paragraphs = [p for p in section.paragraphs if p.text != "독립된 추가 현장 자료는 확인하지 못했다."]
    report.content_hash = None
    with pytest.raises(ValueError):
        validate_neutral_report(report, request)


def test_async_writer_and_injected_llm_receive_only_typed_input():
    class Model:
        async def generate(self, **kwargs):
            assert kwargs["response_model"] is c.DraftReport
            assert "새 사실" in kwargs["system_prompt"]
            assert "draft" in kwargs["payload"]
            result = kwargs["payload"]["draft"]
            result["content_hash"] = None
            return c.DraftReport.model_validate(result)
    request = writer_request()
    report = asyncio.run(NeutralWriter(llm=Model(), model="fixture-model")(request))
    assert report == build_neutral_report(request)
    assert asyncio.run(NeutralWriter()(request)) == report


def test_markdown_escapes_untrusted_claim_markup():
    request = writer_request()
    claim = request.perspective_results[c.PERSPECTIVES[0]].claims[0]
    claim.text = "<script>raw & text</script> [fake]"
    request.claim_supports[0].claim_hash = c.canonical_hash(claim)
    request.perspective_results[c.PERSPECTIVES[0]].result_hash = None
    result = c.PerspectiveResult.model_validate(request.perspective_results[c.PERSPECTIVES[0]].model_dump(mode="json"))
    request.perspective_results[c.PERSPECTIVES[0]] = result
    request.gate.perspective_hashes[c.PERSPECTIVES[0]] = result.result_hash
    md = neutral_markdown(build_neutral_report(request))
    assert "<script>" not in md and "&lt;script&gt;" in md
