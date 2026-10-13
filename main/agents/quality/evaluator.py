"""Hybrid report evaluator, returning findings for the Supervisor to repair."""

from __future__ import annotations

from main.agents.supervisor import contracts as c

from collections.abc import Callable
import re
from typing import Any

from .claim_support import ClaimSupportVerifier
from .rubrics import (
    CRITERIA, NEUTRALITY_PATTERN, QUESTIONS, REPORT_FIELDS, REPORT_JUDGE_SCHEMA,
    RUBRIC_VERSION, SECTION_ORDER, QualityInputError, call_async, current_support,
    digest, has_external_original, independent_groups, index, parse_date, prompt, quote_hash, record, validate_response,
)


class ReportEvaluator:
    """Async ``EvaluatorRequest -> EvalVerdict`` worker.

    Request approval and report/epoch currentness are Supervisor responsibilities.
    This service recomputes content_hash and binds all three report identifiers in
    its output. It never issues a gate, updates State, or renders a PDF.
    """

    def __init__(self, *, verifier: ClaimSupportVerifier, judge: Callable,
                 company_id: str, judge_model: str, timeout_seconds: float = 30):
        self.verifier = verifier
        self.judge = judge
        self.company_id = company_id
        self.judge_model = judge_model
        self.timeout_seconds = timeout_seconds

    async def __call__(self, request: Any):
        data = record(c.EvaluatorRequest.model_validate(record(request)))
        report = data["report"]
        claims = index(data["claims"], "claim_id")
        evidence = index(data["evidence"], "evidence_id")
        sources = index(data["sources"], "source_id")
        sections = report["sections"]
        section_by_id = index(sections, "section_id")
        paragraphs = [paragraph for section in sections for paragraph in section["paragraphs"]]
        paragraph_by_id = index(paragraphs, "paragraph_id")
        checks = {"report_integrity": True, "section_order": True, "groundedness": True,
                  "neutrality": True, "bias_control": True, "perspective_coverage": True,
                  "reference_integrity": True, "reference_format": True, "judge_response": False}
        findings = []

        def fail(check, criterion, reason, *, section_id="SUMMARY", claim_id=None,
                 evidence_ids=(), target=None, repair="rewrite"):
            checks[check] = False
            findings.append({"criterion": criterion, "severity": "critical", "section_id": section_id,
                             "claim_id": claim_id, "evidence_ids": list(evidence_ids), "reason": reason[:2000],
                             "repair_kind": repair, "target_perspective": target})

        expected_hash = digest(report, REPORT_FIELDS, c.canonical_hash)
        if report.get("content_hash") != expected_hash:
            fail("report_integrity", "groundedness", "Report content_hash differs from its content")
        if tuple(section_by_id) != SECTION_ORDER or (sections and (
                sections[0]["title"] != "SUMMARY" or sections[-1]["title"] != "REFERENCE")):
            fail("section_order", "perspective_coverage", "Use the eight fixed sections in SDD order")
        used_claims = {cid for paragraph in paragraphs for cid in paragraph["claim_ids"]}
        if used_claims != set(report["claim_ids"]) or len(report["claim_ids"]) != len(set(report["claim_ids"])):
            fail("report_integrity", "groundedness", "Report claim_ids differ from paragraph claims")

        supports, original_excerpts = {}, []
        for cid, claim in claims.items():
            if claim["kind"] == "unknown":
                continue
            support = await self.verifier.verify(claim, evidence=data["evidence"], sources=data["sources"],
                                                 company_id=self.company_id, as_of_date=data["as_of_date"])
            supports[cid] = support
            if current_support(support, claim, evidence, c.canonical_hash, self.verifier.verifier_version):
                original_excerpts.append(await self.verifier.prepare(
                    claim, evidence=data["evidence"], sources=data["sources"],
                    company_id=self.company_id, as_of_date=data["as_of_date"]))
            elif cid in used_claims:
                target = claim["perspective"]
                fail("groundedness", "groundedness", record(support)["reason"],
                     section_id=target, claim_id=cid, evidence_ids=claim["evidence_ids"],
                     target=target, repair="research")

        body_citations = [citation for citation in report["citations"]
                          if citation["paragraph_id"] not in {
                              p["paragraph_id"] for p in section_by_id.get("REFERENCE", {}).get("paragraphs", [])}]
        citation_keys = [(c["source_id"], c["evidence_id"], c["paragraph_id"]) for c in report["citations"]]
        if len(citation_keys) != len(set(citation_keys)):
            fail("groundedness", "groundedness", "Duplicate citation links")
        for citation in report["citations"]:
            ev = evidence.get(citation["evidence_id"])
            paragraph = paragraph_by_id.get(citation["paragraph_id"])
            if (not ev or ev["source_id"] != citation["source_id"]
                    or citation["source_id"] not in sources or not paragraph
                    or citation["evidence_id"] not in paragraph["evidence_ids"]):
                fail("groundedness", "groundedness", "Citation does not link its original source and paragraph")
        for section in sections:
            sid = section["section_id"]
            if NEUTRALITY_PATTERN.search(section["title"]):
                fail("neutrality", "neutrality", "Section title contains recommendation or superiority wording", section_id=sid)
            for paragraph in section["paragraphs"]:
                pids, eids = paragraph["claim_ids"], paragraph["evidence_ids"]
                linked = [claims[cid] for cid in pids if cid in claims]
                approved_evidence = {eid for claim in linked for eid in claim["evidence_ids"]}
                if paragraph["kind"] in {"fact", "inference"}:
                    cited = {c["evidence_id"] for c in report["citations"]
                             if c["paragraph_id"] == paragraph["paragraph_id"]}
                    if (not pids or not eids or len(linked) != len(pids)
                            or any(claim["kind"] == "unknown" for claim in linked)
                            or not set(eids) <= approved_evidence or not set(eids) <= evidence.keys()
                            or cited != set(eids)):
                        fail("groundedness", "groundedness", "Factual paragraph lacks approved claims and exact citations", section_id=sid)
                elif paragraph["kind"] == "connective" and (pids or eids):
                    fail("groundedness", "groundedness", "Connective paragraph introduces factual links", section_id=sid)
                if NEUTRALITY_PATTERN.search(paragraph["text"]):
                    fail("neutrality", "neutrality", "Recommendation, ranking, investment score or superiority wording", section_id=sid)
        global_sources = set()
        for perspective in QUESTIONS:
            section = section_by_id.get(perspective, {"paragraphs": []})
            linked_ids = {cid for p in section["paragraphs"] if p["kind"] in {"fact", "inference"}
                          for cid in p["claim_ids"]}
            supported_ids = {cid for cid in linked_ids if cid in claims and cid in supports
                             and claims[cid]["perspective"] == perspective
                             and current_support(supports[cid], claims[cid], evidence, c.canonical_hash,
                                                 self.verifier.verifier_version)}
            if len(supported_ids) < 2:
                existing = {cid for cid, claim in claims.items() if claim["perspective"] == perspective
                            and cid in supports and current_support(supports[cid], claim, evidence,
                                                                   c.canonical_hash, self.verifier.verifier_version)}
                research = len(existing) < 2
                fail("perspective_coverage", "perspective_coverage", "Perspective needs two substantive supported claims", section_id=perspective,
                     target=perspective if research else None, repair="research" if research else "rewrite")
            cited_ids = {eid for p in section["paragraphs"] for eid in p["evidence_ids"]
                         if any(eid in claims[cid]["evidence_ids"] for cid in supported_ids)}
            used_sources = {evidence[eid]["source_id"] for eid in cited_ids if eid in evidence}
            global_sources.update(used_sources)
            selected = [sources[sid] for sid in sorted(used_sources) if sid in sources]
            if independent_groups(selected) < 2 or not has_external_original(selected, data["sources"]):
                # Existing approved material can sometimes fix a biased report by rewriting.
                available_ids = {eid for cid, claim in claims.items() if claim["perspective"] == perspective
                                 and cid in supports and current_support(supports[cid], claim, evidence,
                                                                        c.canonical_hash, self.verifier.verifier_version)
                                 for eid in claim["evidence_ids"]}
                available = {evidence[eid]["source_id"] for eid in available_ids}
                all_sources = [sources[sid] for sid in sorted(available)]
                research = independent_groups(all_sources) < 2 or not has_external_original(all_sources, data["sources"])
                fail("bias_control", "bias_control", "Perspective lacks two independent groups including an external source",
                     section_id=perspective, target=perspective if research else None,
                     repair="research" if research else "rewrite")
        if independent_groups([sources[sid] for sid in sorted(global_sources)]) < 3:
            fail("bias_control", "bias_control", "Report needs three independent source groups across its perspectives")
        if not section_by_id.get("limitations", {}).get("paragraphs"):
            fail("bias_control", "bias_control", "Report is missing verification limits", section_id="limitations")
        referenced = set()
        reference_text = {}
        for paragraph in section_by_id.get("REFERENCE", {}).get("paragraphs", []):
            for eid in paragraph["evidence_ids"]:
                if eid not in evidence:
                    fail("reference_integrity", "groundedness", "REFERENCE contains unknown evidence", section_id="REFERENCE")
                else:
                    source_id = evidence[eid]["source_id"]
                    referenced.add(source_id)
                    reference_text.setdefault(source_id, []).append(paragraph["text"])
        if referenced != {citation["source_id"] for citation in body_citations}:
            fail("reference_integrity", "groundedness", "REFERENCE source set differs from sources actually cited in the body", section_id="REFERENCE")
        for source_id, texts in reference_text.items():
            source = sources[source_id]
            text = " ".join(texts)
            locator = source.get("canonical_url") or source.get("original_file_id")
            required = [source["title"], *([source["publisher"]] if source.get("publisher") else []),
                        *([locator] if locator else [])]
            date_text = text
            for value in required:
                date_text = date_text.replace(value, "")
            published = source.get("published_at")
            date_present = (bool(re.search(rf"(?<!\d){parse_date(published).year}(?!\d)", date_text))
                            if published else "발행일 미상" in date_text
                            or "publication date unknown" in date_text.casefold())
            if not all(value in text for value in required) or not date_present:
                fail("reference_format", "groundedness", f"REFERENCE lacks confirmed publisher/title/date/locator for {source_id}", section_id="REFERENCE")

        # Include uncited contrary evidence too, so a favorable selection is detectable.
        all_original_evidence = []
        for ev in data["evidence"]:
            try:
                all_original_evidence.append(await self.verifier.resolve_evidence(
                    ev, sources[ev["source_id"]], company_id=self.company_id, as_of_date=data["as_of_date"]))
            except Exception as exc:
                all_original_evidence.append({"evidence": ev, "resolution_error": type(exc).__name__})
                fail("bias_control", "bias_control", f"Cannot verify original bundle evidence {ev['evidence_id']} ({type(exc).__name__}); reevaluation is required")

        scores = {criterion: 0 for criterion in CRITERIA}
        judge_prompt = prompt("report_judge")
        try:
            response = await call_async(
                self.judge, prompt=judge_prompt, schema=REPORT_JUDGE_SCHEMA,
                payload={"report": report, "claims": list(claims.values()), "evidence": data["evidence"],
                         "sources": data["sources"], "original_excerpts": original_excerpts,
                         "all_original_evidence": all_original_evidence,
                         "claim_supports": [record(value) for value in supports.values()],
                         "required_questions": QUESTIONS, "as_of_date": data["as_of_date"],
                         "structural_checks": checks}, timeout_seconds=self.timeout_seconds,
            )
            response = validate_response(response, REPORT_JUDGE_SCHEMA)
            # Validate every finding before accepting any Judge score or finding.
            for finding in response["findings"]:
                if (finding["section_id"] not in section_by_id
                        or finding["claim_id"] is not None and finding["claim_id"] not in claims
                        or not set(finding["evidence_ids"]) <= evidence.keys()
                        or finding["repair_kind"] == "research" and finding["target_perspective"] not in QUESTIONS
                        or finding["repair_kind"] != "research" and finding["target_perspective"] is not None):
                    raise QualityInputError("Judge finding has invalid references or repair target")
            scores = response["criterion_scores"]
            findings.extend(response["findings"])
            checks["judge_response"] = True
            for criterion, score in scores.items():
                if score < 3 and not any(f["criterion"] == criterion and f["severity"] == "critical" for f in findings):
                    findings.append({"criterion": criterion, "severity": "critical", "section_id": "SUMMARY",
                                     "claim_id": None, "evidence_ids": [], "reason": f"Judge score {score} is below the required 3",
                                     "repair_kind": "rewrite", "target_perspective": None})
        except Exception as exc:
            # Judge unavailable/invalid: the report must be evaluated again after repair.
            scores = {criterion: 0 for criterion in CRITERIA}
            fail("judge_response", "groundedness", f"Report Judge failed ({type(exc).__name__})")
        passed = (all(checks.values()) and all(score >= 3 for score in scores.values())
                  and not any(f["severity"] == "critical" for f in findings))
        repair = "none" if passed else "research" if any(f["repair_kind"] == "research" for f in findings) else "rewrite"
        return c.EvalVerdict.model_validate({
            "report_revision": report["revision"], "report_hash": report["content_hash"],
            "evidence_epoch": report["evidence_epoch"], "structural_checks": checks,
            "criterion_scores": scores, "findings": findings, "overall_pass": passed,
            "repair_kind": repair, "rubric_version": RUBRIC_VERSION,
            "judge_model": self.judge_model, "prompt_hash": quote_hash(judge_prompt),
        })
