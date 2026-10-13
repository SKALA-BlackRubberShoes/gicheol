"""Extractive neutral writer: approved text and provenance survive every edit."""
from __future__ import annotations

from pathlib import Path

from main.agents.supervisor import contracts as c


TITLES = {
    c.SectionId.SUMMARY: "SUMMARY",
    c.SectionId.COMPANY_SCOPE: "기업 및 평가 범위",
    c.SectionId.TECHNOLOGY_MATURITY: "기술 성숙도",
    c.SectionId.MARKET: "시장성",
    c.SectionId.STAKEHOLDERS: "이해관계자",
    c.SectionId.DOMAIN_APPLICATION: "도메인 적용",
    c.SectionId.LIMITATIONS: "한계와 추가 확인 사항",
    c.SectionId.REFERENCE: "REFERENCE",
}


def reference_text(source: c.Source) -> str:
    """Use only metadata present in Source; never invent author or journal data."""
    published = source.published_at.isoformat() if source.published_at else "발행일 미상"
    origin = str(source.canonical_url) if source.canonical_url else source.original_file_id
    return f"[{source.source_id}] {source.publisher or '발행처 미상'}. {published}. {source.title}. {origin}"


def approved_claims(request: c.WriterRequest) -> list[c.Claim]:
    return c.deduplicate_claims(request.eligibility.claims + [
        claim for result in request.perspective_results.values() for claim in result.claims
    ])


def validate_writer_input(request: c.WriterRequest) -> c.WriterRequest:
    # Revalidate serialized DTOs: nested Pydantic instances can be mutated later.
    request = c.WriterRequest.model_validate(request.model_dump(mode="json"))
    claims = approved_claims(request)
    c.validate_evidence_bundle(company_id=request.company_id, sources=request.sources,
                              evidence=request.evidence, claims=claims)
    c.validate_claim_supports(claims=claims, evidence=request.evidence,
                             supports=request.claim_supports)
    supports = {item.claim_id: item for item in request.claim_supports}
    evidence = {item.evidence_id: item for item in request.evidence}
    sources = {item.source_id: item for item in request.sources}
    perspective_ids = {cl.claim_id for result in request.perspective_results.values() for cl in result.claims}
    for claim in claims:
        # Eligibility claims are approved by the current eligibility gate. The
        # shared assessor supplies ClaimSupport for perspective claims only.
        if claim.kind != "unknown" and claim.claim_id in perspective_ids:
            support = supports.get(claim.claim_id)
            if support is None or support.support_status != "supported" or set(support.evidence_hashes) != set(claim.evidence_ids):
                raise ValueError("writer requires current support for every cited claim and evidence")
        for eid in claim.evidence_ids:
            item, source = evidence[eid], sources[evidence[eid].source_id]
            if (item.event_date and item.event_date > request.as_of_date) or (source.published_at and source.published_at > request.as_of_date):
                raise ValueError("writer evidence is later than the assessment date")
    for perspective in c.PERSPECTIVES:
        result = request.perspective_results[perspective]
        if any(g.severity == c.Severity.CRITICAL for g in result.gaps):
            raise ValueError("writer cannot consume unresolved critical gaps")
        for question in c.REQUIRED_QUESTIONS[perspective]:
            if not any(cid in supports and supports[cid].support_status == "supported"
                       for cid in result.answered_questions.get(question, [])):
                raise ValueError("writer requires supported answers to each fixed question")
        if sum(cl.kind != "unknown" for cl in result.claims) < 2 or not result.limitations:
            raise ValueError("writer requires two supported claims and limitations per perspective")
    return request


def _claim_paragraph(claim: c.Claim, paragraph_id: str) -> c.ReportParagraph:
    return c.ReportParagraph(paragraph_id=paragraph_id, text=claim.text, kind=claim.kind,
                             claim_ids=[claim.claim_id], evidence_ids=claim.evidence_ids)


def build_neutral_report(request: c.WriterRequest) -> c.DraftReport:
    """Build all sections deterministically without an investment decision path."""
    request = validate_writer_input(request)
    paragraphs = {sid: [] for sid in c.SECTION_ORDER}
    for index, claim in enumerate(request.eligibility.claims):
        paragraphs[c.SectionId.COMPANY_SCOPE].append(_claim_paragraph(claim, f"scope-{index}"))
        for j, limitation in enumerate(claim.limitations):
            paragraphs[c.SectionId.LIMITATIONS].append(c.ReportParagraph(
                paragraph_id=f"limit-scope-{index}-{j}", text=limitation,
                kind="unknown", claim_ids=[claim.claim_id], evidence_ids=[]))
    for perspective in c.PERSPECTIVES:
        result = request.perspective_results[perspective]
        sid = c.SectionId(perspective.value)
        for index, claim in enumerate(result.claims):
            paragraphs[sid].append(_claim_paragraph(claim, f"{perspective.value}-{index}"))
            for j, limitation in enumerate(claim.limitations):
                paragraphs[c.SectionId.LIMITATIONS].append(c.ReportParagraph(
                    paragraph_id=f"limit-{perspective.value}-{index}-{j}", text=limitation,
                    kind="unknown", claim_ids=[claim.claim_id], evidence_ids=[]))
        first = next(cl for cl in result.claims if cl.kind != "unknown")
        paragraphs[c.SectionId.SUMMARY].append(_claim_paragraph(first, f"summary-{perspective.value}"))
        paragraphs[c.SectionId.SUMMARY].append(c.ReportParagraph(
            paragraph_id=f"summary-limit-{perspective.value}", text=result.limitations[0],
            kind="unknown", claim_ids=[], evidence_ids=[]))
        for index, limitation in enumerate(result.limitations):
            paragraphs[c.SectionId.LIMITATIONS].append(c.ReportParagraph(
                paragraph_id=f"limit-{perspective.value}-{index}", text=limitation,
                kind="unknown", claim_ids=[], evidence_ids=[]))
    paragraphs[c.SectionId.COMPANY_SCOPE].append(c.ReportParagraph(
        paragraph_id="assessment-scope", kind="connective", claim_ids=[], evidence_ids=[],
        text=f"평가 대상 식별자: {request.company_id}. 평가 기준일: {request.as_of_date.isoformat()}."))
    by_evidence = {item.evidence_id: item for item in request.evidence}
    citations = [c.Citation(source_id=by_evidence[eid].source_id, evidence_id=eid,
                            paragraph_id=p.paragraph_id)
                 for entries in paragraphs.values() for p in entries for eid in p.evidence_ids]
    cited_sources = {item.source_id for item in citations}
    for source in sorted(request.sources, key=lambda item: item.source_id):
        if source.source_id in cited_sources:
            paragraphs[c.SectionId.REFERENCE].append(c.ReportParagraph(
                paragraph_id=f"reference-{len(paragraphs[c.SectionId.REFERENCE])}", text=reference_text(source),
                kind="connective", claim_ids=[], evidence_ids=[]))
    report = c.DraftReport(revision=request.report_revision, evidence_epoch=request.evidence_epoch,
        sections=[c.ReportSection(section_id=sid, title=TITLES[sid], paragraphs=paragraphs[sid])
                  for sid in c.SECTION_ORDER],
        claim_ids=sorted({cid for entries in paragraphs.values() for p in entries for cid in p.claim_ids}),
        citations=citations)
    validate_neutral_report(report, request)
    return report


def validate_references(report: c.DraftReport, sources: list[c.Source], evidence: list[c.Evidence]) -> None:
    by_source = {s.source_id: s for s in sources}
    by_evidence = {e.evidence_id: e for e in evidence}
    if len(by_source) != len(sources) or len(by_evidence) != len(evidence):
        raise ValueError("duplicate source or evidence identity")
    for citation in report.citations:
        item = by_evidence.get(citation.evidence_id)
        if item is None or item.source_id != citation.source_id or citation.source_id not in by_source:
            raise ValueError("citation source does not own evidence")
    expected = [reference_text(by_source[sid]) for sid in sorted({item.source_id for item in report.citations})]
    refs = report.sections[-1].paragraphs
    if [p.text for p in refs] != expected or any(p.claim_ids or p.evidence_ids or p.kind != "connective" for p in refs):
        raise ValueError("REFERENCE must exactly match the body citation source set and metadata")


def validate_neutral_report(report: c.DraftReport, request: c.WriterRequest) -> c.DraftReport:
    report = c.DraftReport.model_validate(report.model_dump(mode="json"))
    if (report.revision, report.evidence_epoch) != (request.report_revision, request.evidence_epoch):
        raise ValueError("writer output revision or epoch differs from its request")
    if report.document_ref is not None:
        raise ValueError("writer must not invent a stored document reference")
    claims = approved_claims(request)
    by_claim = {cl.claim_id: cl for cl in claims}
    template = {f"평가 대상 식별자: {request.company_id}. 평가 기준일: {request.as_of_date.isoformat()}."}
    limitations = {text for cl in claims for text in cl.limitations}
    limitations.update(text for result in request.perspective_results.values() for text in result.limitations)
    c.EvaluatorRequest(task_id=request.task_id, report=report, claims=claims,
                       evidence=request.evidence, sources=request.sources, as_of_date=request.as_of_date)
    validate_references(report, request.sources, request.evidence)
    for section in report.sections[:-1]:
        if section.title != TITLES[section.section_id] or not section.paragraphs:
            raise ValueError("writer must preserve section headings and coverage")
        for paragraph in section.paragraphs:
            if paragraph.kind == "connective":
                if paragraph.text not in template or section.section_id != c.SectionId.COMPANY_SCOPE:
                    raise ValueError("writer introduced an unapproved connective statement")
            elif paragraph.kind == "unknown" and paragraph.text in limitations and section.section_id in {c.SectionId.LIMITATIONS, c.SectionId.SUMMARY}:
                if paragraph.evidence_ids:
                    raise ValueError("limitation must not add citations")
                if paragraph.claim_ids and not any(paragraph.text in by_claim[cid].limitations for cid in paragraph.claim_ids):
                    raise ValueError("limitation refers to an unrelated claim")
            else:
                if len(paragraph.claim_ids) != 1 or paragraph.claim_ids[0] not in by_claim:
                    raise ValueError("writer introduced an unapproved claim")
                claim = by_claim[paragraph.claim_ids[0]]
                if (paragraph.text, paragraph.kind, paragraph.evidence_ids) != (claim.text, claim.kind, claim.evidence_ids):
                    raise ValueError("writer changed approved claim text, kind or evidence")
                if section.section_id.value in {p.value for p in c.PERSPECTIVES} and claim.perspective.value != section.section_id.value:
                    raise ValueError("writer put claim under the wrong perspective")
    for perspective in c.PERSPECTIVES:
        section = next(s for s in report.sections if s.section_id.value == perspective.value)
        covered = {cid for p in section.paragraphs for cid in p.claim_ids}
        if not {cl.claim_id for cl in request.perspective_results[perspective].claims} <= covered:
            raise ValueError("writer dropped approved perspective claims")
        summary_claims = {cid for p in report.sections[0].paragraphs if p.kind in {"fact", "inference"} for cid in p.claim_ids}
        if not summary_claims.intersection(cl.claim_id for cl in request.perspective_results[perspective].claims):
            raise ValueError("SUMMARY must include an approved result from each perspective")
    if not {cl.claim_id for cl in request.eligibility.claims} <= set(report.claim_ids):
        raise ValueError("writer dropped eligibility claims")
    if not limitations <= {p.text for s in report.sections for p in s.paragraphs}:
        raise ValueError("writer dropped approved limitations")
    if report.sections[-1].title != TITLES[c.SectionId.REFERENCE]:
        raise ValueError("writer changed REFERENCE heading")
    return report


class NeutralWriter:
    """Async DTO worker. Optional model edits only select/reorder existing text."""
    def __init__(self, *, llm: c.LLMClient | None = None, model: str | None = None):
        if llm is not None and not model:
            raise ValueError("a configured report model is required with LLMClient")
        self.llm, self.model = llm, model

    async def __call__(self, request: c.WriterRequest) -> c.DraftReport:
        request = validate_writer_input(request)
        report = build_neutral_report(request)
        if self.llm is not None:
            prompt = (Path(__file__).parents[2] / "prompts/neutral_report/writer.txt").read_text(encoding="utf-8")
            edited = await self.llm.generate(model=self.model, system_prompt=prompt,
                payload={"request": request.model_dump(mode="json"), "draft": report.model_dump(mode="json")},
                response_model=c.DraftReport)
            report = validate_neutral_report(edited, request)
        return report
