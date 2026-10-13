"""Four-perspective evidence gate service; no State or GateCertificate writes."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .claim_support import ClaimSupportVerifier
from .rubrics import (
    QUESTIONS, RUBRIC_VERSION, SUFFICIENCY_JUDGE_SCHEMA, QualityInputError,
    call_async, current_support, has_external_original, independent_groups, index, make_gap, merge_gaps, prompt,
    record, validate_response,
)


class SufficiencyAssessor:
    """Async ``SufficiencyRequest -> list[SufficiencyVerdict]`` service.

    The run's company and corpus/index versions are fixed at construction.
    save_claim_support(payload, key=...) must atomically save and return a
    hash-checked ArtifactRef. receipt_validator checks actual retrieval records.
    """

    def __init__(self, *, verifier: ClaimSupportVerifier, judge: Callable,
                 save_claim_support: Callable, receipt_validator: Callable,
                 contract_factory: Callable, hash_fn: Callable, semantic_hash_fn: Callable,
                 company_id: str, corpus_version: str, index_version: str,
                 timeout_seconds: float = 30):
        self.verifier = verifier
        self.judge = judge
        self.save_claim_support = save_claim_support
        self.receipt_validator = receipt_validator
        self.contract_factory = contract_factory
        self.hash_fn = hash_fn
        self.semantic_hash_fn = semantic_hash_fn
        self.company_id = company_id
        self.corpus_version = corpus_version
        self.index_version = index_version
        self.timeout_seconds = timeout_seconds

    async def __call__(self, request: Any):
        data = record(self.contract_factory("SufficiencyRequest", record(request)))
        results = data["perspective_results"]
        if set(results) - QUESTIONS.keys():
            raise QualityInputError("Unexpected perspective result")
        evidence = data["evidence"]
        sources = data["sources"]
        evidence_by_id = index(evidence, "evidence_id")
        source_by_id = index(sources, "source_id")
        index([claim for result in results.values() for claim in result["claims"]], "claim_id")
        eligibility = data["eligibility"]
        # Supervisor routes target retries only from eligibility-scoped gaps.
        # Keep target failures separate from the otherwise valid research views.
        eligibility_gaps = [dict(gap, perspective="eligibility")
                            for gap in eligibility.get("gaps", []) if gap["severity"] == "critical"]
        target_evidence_ids = sorted({eid for claim in eligibility.get("claims", [])
                                      for eid in claim["evidence_ids"]})
        if eligibility.get("verdict") != "eligible":
            eligibility_gaps.append(make_gap("eligibility", "eligibility_unknown",
                f"Confirm target verdict: {eligibility.get('verdict')}", evidence_ids=target_evidence_ids))
        for condition in ("unlisted", "funding_stage", "exit_status"):
            if eligibility.get(condition) is not True:
                eligibility_gaps.append(make_gap("eligibility", "eligibility_unknown",
                    f"Confirm target condition {condition}: {eligibility.get(condition)}", evidence_ids=target_evidence_ids))
        if eligibility.get("result_hash") != self.semantic_hash_fn(eligibility):
            eligibility_gaps.append(make_gap("eligibility", "eligibility_unknown",
                "Refresh changed target result hash", evidence_ids=target_evidence_ids))
        if not eligibility.get("claims"):
            eligibility_gaps.append(make_gap("eligibility", "eligibility_unknown",
                "Provide original evidence for target conditions"))
        for claim in eligibility.get("claims", []):
            support = await self.verifier.verify(claim, evidence=evidence, sources=sources,
                                                 company_id=self.company_id, as_of_date=data["as_of_date"])
            if not current_support(support, claim, evidence_by_id, self.hash_fn,
                                   self.verifier.verifier_version):
                value = record(support)
                status = value["support_status"]
                failure = "stale claim/evidence/verifier binding" if status == "supported" else status
                eligibility_gaps.append(make_gap("eligibility", "eligibility_unknown",
                    f"Verify target claim {claim['claim_id']} ({failure}): {value['reason']}"[:2000],
                    evidence_ids=claim["evidence_ids"], query_hints=[f"Verify target claim {claim['claim_id']}"]))
        eligibility_gaps = merge_gaps(eligibility_gaps)
        views = {}
        for perspective in QUESTIONS:
            result = results.get(perspective)
            gaps, support_refs, supported, excerpts = [], [], {}, []
            used_evidence = set()
            if not result:
                gaps.append(make_gap(perspective, "missing_evidence", "Collect this perspective"))
                result = {"claims": [], "gaps": [], "limitations": [], "evidence_ids": [],
                          "answered_questions": {}, "retrieval_receipts": []}
            else:
                if result.get("perspective") != perspective or result.get("result_hash") != self.semantic_hash_fn(result):
                    gaps.append(make_gap(perspective, "citation_mismatch", "Refresh changed perspective result"))
                for claim in result["claims"]:
                    support = await self.verifier.verify(
                        claim, evidence=evidence, sources=sources,
                        company_id=self.company_id, as_of_date=data["as_of_date"],
                    )
                    support_payload = record(support)
                    saved = await call_async(
                        self.save_claim_support, support_payload,
                        key=f"claim-supports/{self.hash_fn(support_payload)}.json",
                        timeout_seconds=self.timeout_seconds,
                    )
                    ref = self.contract_factory("ArtifactRef", record(saved))
                    support_refs.append(record(ref))
                    if (claim.get("perspective") == perspective
                            and set(claim["evidence_ids"]) <= set(result["evidence_ids"])
                            and current_support(support, claim, evidence_by_id, self.hash_fn,
                                                self.verifier.verifier_version)):
                        supported[claim["claim_id"]] = claim
                        used_evidence.update(claim["evidence_ids"])
                        # Resolve again rather than substitute the worker's summary.
                        excerpts.append(await self.verifier.prepare(
                            claim, evidence=evidence, sources=sources,
                            company_id=self.company_id, as_of_date=data["as_of_date"]))
                    elif claim.get("kind") != "unknown":
                        gaps.append(make_gap(perspective, "citation_mismatch",
                                             support_payload["reason"][:2000],
                                             evidence_ids=claim["evidence_ids"]))
            gaps.extend(eligibility_gaps)
            if len({claim["text"].strip() for claim in supported.values()}) < 2:
                gaps.append(make_gap(perspective, "missing_evidence", "Obtain two distinct supported claims"))
            answers = result["answered_questions"]
            if set(answers) - set(QUESTIONS[perspective]):
                gaps.append(make_gap(perspective, "missing_question", "Use this perspective's fixed question IDs"))
            for question in QUESTIONS[perspective]:
                if not any(cid in supported for cid in answers.get(question, [])):
                    gaps.append(make_gap(perspective, "missing_question", question,
                                         query_hints=[f"Verify {question} for {self.company_id}"]))
            used_sources = {evidence_by_id[eid]["source_id"] for eid in used_evidence}
            original_sources = [source_by_id[sid] for sid in sorted(used_sources)]
            if independent_groups(original_sources) < 2:
                gaps.append(make_gap(perspective, "source_bias", "Obtain two independent original publisher groups"))
            if not has_external_original(original_sources, sources):
                gaps.append(make_gap(perspective, "source_bias", "Obtain an externally controlled source"))
            if not result["limitations"]:
                gaps.append(make_gap(perspective, "missing_evidence", "State verification limits or information gaps"))
            for gap in result["gaps"]:
                gaps.append(gap)
                if gap["kind"] == "conflicting_evidence" and (len(set(gap["evidence_ids"])) < 2
                        or not set(gap["evidence_ids"]) <= set(result["evidence_ids"])):
                    gaps.append(make_gap(perspective, "conflicting_evidence", "Preserve both sides of conflicting evidence"))
                if gap["kind"] == "conflicting_evidence":
                    for eid in gap["evidence_ids"]:
                        try:
                            ev = evidence_by_id[eid]
                            excerpts.append({"conflicting_evidence": await self.verifier.resolve_evidence(
                                ev, source_by_id[ev["source_id"]], company_id=self.company_id,
                                as_of_date=data["as_of_date"])})
                        except Exception:
                            gaps.append(make_gap(perspective, "conflicting_evidence", "Resolve both original conflicting excerpts"))
            if perspective == "technology_maturity" and not await self._has_rag(result, used_evidence, evidence_by_id, source_by_id):
                gaps.append(make_gap(perspective, "missing_evidence", "Use PDF evidence and a verified matching retrieval receipt"))
            views[perspective] = {"result": result, "gaps": gaps, "support_refs": support_refs,
                                  "supported": supported, "excerpts": excerpts,
                                  "evidence_ids": sorted(used_evidence), "source_ids": used_sources}
        global_sources = {sid for view in views.values() for sid in view["source_ids"]}
        if independent_groups([source_by_id[sid] for sid in sorted(global_sources)]) < 3:
            for perspective, view in views.items():
                view["gaps"].append(make_gap(perspective, "source_bias", "Obtain three independent groups across all perspectives"))
        verdicts = []
        for perspective, view in views.items():
            gaps = view["gaps"]
            structural = not any(gap["severity"] == "critical" for gap in gaps)
            semantic, reason = False, "Structural evidence requirements are not met"
            if structural:
                try:
                    response = await call_async(
                        self.judge, prompt=prompt("sufficiency"), schema=SUFFICIENCY_JUDGE_SCHEMA,
                        payload={"perspective": perspective, "required_questions": QUESTIONS[perspective],
                                 "result": view["result"], "verified_claims": list(view["supported"].values()),
                                 "original_excerpts": view["excerpts"], "as_of_date": data["as_of_date"]},
                        timeout_seconds=self.timeout_seconds,
                    )
                    response = validate_response(response, SUFFICIENCY_JUDGE_SCHEMA)
                    for item in response["gaps"]:
                        if not set(item["evidence_ids"]) <= set(view["result"]["evidence_ids"]):
                            raise QualityInputError("Sufficiency Judge invented an evidence ID")
                    judge_gaps = [make_gap(perspective, **item) for item in response["gaps"]]
                    gaps.extend(judge_gaps)
                    semantic = response["semantic_pass"] and not any(gap["severity"] == "critical" for gap in gaps)
                    reason = response["reason"]
                    if not semantic and not any(gap["severity"] == "critical" for gap in gaps):
                        gaps.append(make_gap(perspective, "missing_evidence", reason))
                except Exception as exc:
                    reason = f"Sufficiency Judge failed ({type(exc).__name__})"
                    gaps.append(make_gap(perspective, "missing_evidence", reason))
            payload = {"perspective": perspective, "structural_pass": structural,
                       "semantic_pass": semantic, "gaps": merge_gaps(gaps),
                       "evidence_ids": view["evidence_ids"], "claim_support_refs": view["support_refs"],
                       "reason": reason[:2000], "rubric_version": RUBRIC_VERSION}
            # SufficiencyVerdict.result_hash hashes the verdict itself, not the research result.
            payload["result_hash"] = self.semantic_hash_fn(payload)
            verdicts.append(self.contract_factory("SufficiencyVerdict", payload))
        return verdicts

    async def _has_rag(self, result, used_evidence, evidence_by_id, source_by_id):
        pdf = []
        for eid in used_evidence:
            ev = evidence_by_id[eid]
            source = source_by_id[ev["source_id"]]
            ref = source["content_ref"]
            is_pdf = (ref.get("media_type") == "application/pdf"
                      or (source.get("original_file_id") or "").lower().endswith(".pdf")
                      or (source.get("canonical_url") or "").split("?", 1)[0].lower().endswith(".pdf"))
            if is_pdf and type(ev.get("page_number")) is int and ev["page_number"] >= 1:
                pdf.append(ev)
        for receipt in result["retrieval_receipts"]:
            if (receipt.get("outcome") != "success"
                    or receipt.get("filters", {}).get("company_id") != self.company_id
                    or receipt.get("corpus_version") != self.corpus_version
                    or receipt.get("index_version") != self.index_version
                    or not any(hit["source_id"] == ev["source_id"] and hit["chunk_id"] == ev["chunk_id"]
                               for hit in receipt.get("ranked_hits", []) for ev in pdf)):
                continue
            try:
                if await call_async(self.receipt_validator, receipt, timeout_seconds=self.timeout_seconds) is True:
                    return True
            except Exception:
                continue
        return False
