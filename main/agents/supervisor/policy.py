"""Deterministic routing and approval-currentness policy for the neutral run."""
from collections.abc import Mapping, Sequence
from typing import Any

POLICY_VERSION = "1.0"
PERSPECTIVE_IDS = ("technology_maturity", "market", "stakeholders", "domain_application")
GAP_PRIORITY = {"missing_question": 80, "citation_mismatch": 70, "source_bias": 60}


def terminal_status(reason):
    """Map the SDD's evidence/budget endings separately from service failures."""
    return "incomplete" if reason in {
        "INELIGIBLE_TARGET", "ELIGIBILITY_UNKNOWN", "INSUFFICIENT_EVIDENCE",
        "QUALITY_EXHAUSTED", "BUDGET_EXHAUSTED",
    } else "failed"


def choose_research_target(slots: Mapping, gaps: Sequence) -> str | None:
    """Select unfinished work by gap priority, critical count, then stable ID."""
    candidates = []
    for perspective, slot in slots.items():
        if slot.status == "sufficient":
            continue
        relevant = [g for g in gaps if g.perspective == perspective]
        priority = 100 if slot.status == "pending" else max((GAP_PRIORITY.get(g.kind, 50) for g in relevant), default=50)
        identifier = perspective.value if hasattr(perspective, "value") else str(perspective)
        candidates.append((-priority, -sum(g.severity == "critical" for g in relevant), identifier))
    return min(candidates)[2] if candidates else None


def gate_matches(state: Mapping[str, Any], gate: Any, eligibility_hash: str, rubric_version: str) -> bool:
    return bool(
        state["eligibility_status"] == "eligible"
        and state["evidence_gate_pass"]
        and gate.passed
        and gate.evidence_epoch == state["evidence_epoch"]
        and gate.eligibility_hash == eligibility_hash
        and gate.rubric_version == rubric_version
        and set(gate.perspective_hashes) == set(PERSPECTIVE_IDS)
        and all(slot.status == "sufficient" and gate.perspective_hashes.get(p) == slot.result_hash for p, slot in state["perspectives"].items())
    )


def report_matches(state: Mapping[str, Any], report: Any) -> bool:
    return report.revision == state["report_revision"] and report.evidence_epoch == state["evidence_epoch"]


def evaluation_matches(state: Mapping[str, Any], report: Any, evaluation: Any) -> bool:
    return bool(report_matches(state, report) and evaluation.report_revision == report.revision and evaluation.report_hash == report.content_hash and evaluation.evidence_epoch == state["evidence_epoch"])


def is_pdf(source: Any) -> bool:
    return bool(source.content_ref.media_type == "application/pdf" or (source.original_file_id or "").lower().endswith(".pdf") or (source.canonical_url and str(source.canonical_url).split("?", 1)[0].lower().endswith(".pdf")))


def structural_gaps(company_id, as_of_date, results, verdicts, bundle, supports, eligibility=None):
    """Independently enforce fixed questions, current verifier bindings and sources.

    Semantic decisions belong to the injected assessor. A pass cannot override
    these checks. Broken IDs/hashes are integrity errors; missing usable proof
    becomes a concrete research gap.
    """
    from main.agents.supervisor import contracts as c

    claims = [claim for result in results.values() for claim in result.claims]
    target_claims = list(eligibility.claims) if eligibility else []
    c.validate_evidence_bundle(company_id=company_id, sources=bundle.sources, evidence=bundle.evidence, claims=c.deduplicate_claims(claims + target_claims))
    c.validate_claim_supports(claims=claims, evidence=bundle.evidence, supports=supports)
    sources = {s.source_id: s for s in bundle.sources}
    evidence = {e.evidence_id: e for e in bundle.evidence}
    support_by_claim = {s.claim_id: s for s in supports}
    verdict_by_p = dict(verdicts) if isinstance(verdicts, Mapping) else {v.perspective: v for v in verdicts}
    gaps = []

    def gap(p, kind, question):
        identifier = p.value if hasattr(p, "value") else p
        digest = c.canonical_hash({"perspective": identifier, "kind": kind, "question": question})[:24]
        gaps.append(c.Gap(gap_id=f"gate-{digest}", perspective=p, kind=kind, question=question, severity="critical", query_hints=[question]))

    def usable(e):
        source = sources[e.source_id]
        return bool((e.event_date is None or e.event_date <= as_of_date) and (source.published_at is None or source.published_at <= as_of_date) and e.company_id == company_id)

    if eligibility and (eligibility.verdict != "eligible" or not target_claims or any(cl.kind == "unknown" or not any(usable(evidence[i]) for i in cl.evidence_ids) for cl in target_claims)):
        gap("eligibility", "eligibility_unknown", "Confirm all target conditions with original evidence valid at the assessment date")
    global_groups = set()
    for p in c.PERSPECTIVES:
        result = results.get(p)
        verdict = verdict_by_p.get(p)
        if result is None:
            gap(p, "missing_evidence", "Collect this perspective's original evidence")
            continue
        supported = {}
        used_evidence = set()
        for claim in result.claims:
            support = support_by_claim.get(claim.claim_id)
            if claim.kind in {"fact", "inference"} and support and support.support_status == "supported":
                accepted = [i for i in support.evidence_hashes if i in claim.evidence_ids and usable(evidence[i])]
                if accepted:
                    supported[claim.claim_id] = claim
                    used_evidence.update(accepted)
        if len(supported) < 2:
            gap(p, "missing_evidence", "Provide at least two current, independently verified supported claims")
        for question in c.REQUIRED_QUESTIONS[p]:
            if not any(i in supported for i in result.answered_questions.get(question, [])):
                gap(p, "missing_question", f"Answer fixed question {question.value} with a supported claim")
        used_sources = {evidence[i].source_id for i in used_evidence}
        groups = {sources[i].publisher_group for i in used_sources}
        global_groups.update(groups)
        if len(groups) < 2 or not any(not sources[i].corporate_controlled for i in used_sources):
            gap(p, "source_bias", "Obtain two independent publisher groups including an externally controlled original source")
        if not result.limitations:
            gap(p, "missing_evidence", "State verification limits or information gaps and preserve contradictory sources")
        if verdict is None or not verdict.structural_pass or not verdict.semantic_pass or set(verdict.evidence_ids) - set(result.evidence_ids):
            gap(p, "missing_evidence", "Obtain a current semantic and structural sufficiency verdict")
        gaps.extend(g for g in result.gaps if g.severity == "critical")
        if verdict:
            gaps.extend(g for g in verdict.gaps if g.severity == "critical")
        if p == c.Perspective.TECHNOLOGY_MATURITY:
            pdf_items = [evidence[i] for i in used_evidence if is_pdf(sources[evidence[i].source_id]) and evidence[i].page_number is not None]
            retrieved = any(receipt.outcome == "success" and receipt.filters.get("company_id") == company_id and any(hit.source_id == item.source_id and hit.chunk_id == item.chunk_id for hit in receipt.ranked_hits for item in pdf_items) for receipt in result.retrieval_receipts)
            if not pdf_items or not retrieved:
                gap(p, "missing_evidence", "Use actual PDF evidence with page numbers and a matching successful retrieval receipt")
    if len(global_groups) < 3:
        for p in c.PERSPECTIVES:
            gap(p, "source_bias", "Use at least three independent publisher groups across the complete evaluation")
    return list({g.gap_id: g for g in gaps}.values())
