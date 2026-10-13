"""Synthetic evidence and deterministic adapters; never a live model benchmark."""

from copy import deepcopy
import importlib.util
import os
from pathlib import Path
import sys

from main.agents.quality.claim_support import ClaimSupportVerifier
from main.agents.quality.evaluator import ReportEvaluator
from main.agents.quality.sufficiency import SufficiencyAssessor
from main.agents.quality.rubrics import (
    CRITERIA, QUESTIONS, SECTION_ORDER, canonical_hash, quote_hash, record, semantic_hash,
)

COMMON = None
if os.environ.get("HYOJIN_CONTRACTS_PATH"):
    spec = importlib.util.spec_from_file_location("hyojin_common_contracts", os.environ["HYOJIN_CONTRACTS_PATH"])
    COMMON = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = COMMON
    spec.loader.exec_module(COMMON)


def factory(name, payload):
    if COMMON is not None:
        return getattr(COMMON, name).model_validate(payload)
    # Explicit fixture factory, not a runtime fallback or duplicated schema.
    return deepcopy(payload)


def hash_fn(value):
    return COMMON.canonical_hash(value) if COMMON else canonical_hash(value)


def semantic_hash_fn(value):
    return COMMON.semantic_hash(value) if COMMON else semantic_hash(value)


def fixture():
    texts = {
        "technology_maturity": ["Alpha uses a learned perception model to detect objects.",
                                "Alpha achieved 90 percent detection accuracy in a 2026 lab test."],
        "market": ["Alpha's paying customers are warehouse operators.",
                   "Alpha charges customers a monthly software subscription."],
        "stakeholders": ["Alpha's operators and maintenance partners share deployment tasks.",
                         "Alpha's users need two days of safety training before deployment."],
        "domain_application": ["Alpha ran a warehouse robot pilot at Site A in 2026.",
                               "Alpha's pilot required indoor maps and supervised operation."],
    }
    target = "Alpha was unlisted, at Seed stage and had not exited as of September 2026."
    all_text = "\n".join([sentence for pair in texts.values() for sentence in pair] + [target])
    sources, bodies = [], {}
    for sid, group, controlled, source_type in (
            ("S-company", "alpha", True, "company"),
            ("S-institution", "institute", False, "institution"),
            ("S-paper", "university", False, "paper")):
        body = f"Original publisher: {group}\n{all_text}"
        bodies[sid] = body
        sha = quote_hash(body)
        sources.append({"source_id": sid, "canonical_url": f"https://example.org/{sid}",
                        "original_file_id": "study.pdf" if sid == "S-paper" else None,
                        "title": f"Alpha 2026 {group} study", "publisher": group,
                        "publisher_group": group, "source_type": source_type,
                        "published_at": "2026-09-01", "retrieved_at": "2026-10-13T01:00:00+00:00",
                        "corporate_controlled": controlled, "sha256": sha,
                        "content_ref": {"relative_path": f"sources/{sid}.txt", "sha256": sha,
                                        "media_type": "application/pdf" if sid == "S-paper" else "text/plain",
                                        "schema_version": "1.0"}})
    results, evidence, claims = {}, [], []
    for perspective, pair in texts.items():
        pair_claims, pair_evidence = [], []
        for n, text in enumerate(pair):
            cid, eid = f"C-{perspective}-{n}", f"E-{perspective}-{n}"
            sid = "S-company" if n == 0 else "S-paper" if perspective in {"technology_maturity", "domain_application"} else "S-institution"
            ev = {"evidence_id": eid, "source_id": sid, "company_id": "alpha", "chunk_id": f"chunk-{eid}",
                  "page_number": 1 if sid == "S-paper" else None, "quote": text,
                  "quote_hash": quote_hash(text), "event_date": "2026-09-01"}
            cl = {"claim_id": cid, "perspective": perspective, "text": text, "kind": "fact",
                  "evidence_ids": [eid], "limitations": ["Only the documented pilot/lab scope is verified."]}
            evidence.append(ev)
            claims.append(cl)
            pair_claims.append(cl)
            pair_evidence.append(eid)
        receipts = []
        if perspective == "technology_maturity":
            receipts.append({"query_id": "query-tech", "query_text": "Alpha lab validation",
                             "queries_hash": hash_fn(["Alpha lab validation"]), "provider": "qdrant",
                             "corpus_version": "corpus-1", "index_version": "index-1",
                             "filters": {"company_id": "alpha", "domain": "Physical AI / Robotics"},
                             "ranked_hits": [{"source_id": "S-paper", "chunk_id": f"chunk-{pair_evidence[1]}",
                                              "rank": 1, "score": 0.9}], "outcome": "success",
                             "ts": "2026-10-13T01:00:00+00:00"})
        result = {"task_id": f"task-{perspective}-1", "perspective": perspective, "attempt": 1,
                  "claims": pair_claims, "evidence_ids": pair_evidence,
                  "answered_questions": {qid: [pair_claims[n]["claim_id"]] for n, qid in enumerate(QUESTIONS[perspective])},
                  "gaps": [], "limitations": ["No conflicting evidence found within the supplied search scope; wider deployment is unverified."],
                  "retrieval_receipts": receipts}
        result["result_hash"] = semantic_hash_fn(result)
        results[perspective] = result
    eid = "E-eligibility"
    evidence.append({"evidence_id": eid, "source_id": "S-institution", "company_id": "alpha",
                     "chunk_id": "chunk-target", "page_number": None, "quote": target,
                     "quote_hash": quote_hash(target), "event_date": "2026-09-01"})
    target_claim = {"claim_id": "C-eligibility", "perspective": "market", "text": target,
                    "kind": "fact", "evidence_ids": [eid], "limitations": ["Eligibility is bound to the cutoff date."]}
    claims.append(target_claim)
    eligibility = {"task_id": "eligibility-1", "unlisted": True, "funding_stage": True, "exit_status": True,
                   "verdict": "eligible", "claims": [target_claim], "gaps": []}
    eligibility["result_hash"] = semantic_hash_fn(eligibility)
    sections, citations = [], []
    for sid in SECTION_ORDER:
        section_claims = (results[sid]["claims"] if sid in results else
                          [results["technology_maturity"]["claims"][1]] if sid == "SUMMARY" else
                          [target_claim] if sid == "company_scope" else [])
        paragraphs = []
        for n, claim in enumerate(section_claims):
            pid = f"P-{sid}-{n}"
            paragraphs.append({"paragraph_id": pid, "text": claim["text"], "kind": claim["kind"],
                               "claim_ids": [claim["claim_id"]], "evidence_ids": claim["evidence_ids"]})
            for evidence_id in claim["evidence_ids"]:
                ev = next(item for item in evidence if item["evidence_id"] == evidence_id)
                citations.append({"source_id": ev["source_id"], "evidence_id": evidence_id, "paragraph_id": pid})
        if sid == "limitations":
            paragraphs = [{"paragraph_id": "P-limitations", "text": "Deployment scale remains unverified; no conflict was found within the documented search scope.",
                           "kind": "unknown", "claim_ids": [], "evidence_ids": []}]
        if sid == "REFERENCE":
            for source in sources:
                ev = next(item for item in evidence if item["source_id"] == source["source_id"])
                paragraphs.append({"paragraph_id": f"P-ref-{source['source_id']}",
                                   "text": f"{source['publisher']} (2026). {source['title']}. {source['canonical_url']}",
                                   "kind": "unknown", "claim_ids": [], "evidence_ids": [ev["evidence_id"]]})
        sections.append({"section_id": sid, "title": sid if sid in {"SUMMARY", "REFERENCE"} else sid.replace("_", " "),
                         "paragraphs": paragraphs})
    report = {"revision": 3, "evidence_epoch": 4, "sections": sections,
              "claim_ids": list(dict.fromkeys(cid for s in sections for p in s["paragraphs"] for cid in p["claim_ids"])),
              "citations": citations, "document_ref": None}
    report["content_hash"] = hash_fn({key: report[key] for key in ("sections", "claim_ids", "citations", "evidence_epoch")})
    return {"sources": sources, "bodies": bodies, "evidence": evidence, "claims": claims,
            "perspective_results": results, "eligibility": eligibility, "report": report}


def sufficiency_request(case):
    return {"eligibility": case["eligibility"], "perspective_results": case["perspective_results"],
            "sources": case["sources"], "evidence": case["evidence"], "claim_supports": [],
            "as_of_date": "2026-10-13", "evidence_epoch": 4}


def evaluator_request(case):
    return {"task_id": "evaluate-3", "report": case["report"], "claims": case["claims"],
            "evidence": case["evidence"], "sources": case["sources"], "as_of_date": "2026-10-13"}


def rehash(case):
    for result in case["perspective_results"].values():
        result["result_hash"] = semantic_hash_fn(result)
    case["eligibility"]["result_hash"] = semantic_hash_fn(case["eligibility"])
    report = case["report"]
    report["content_hash"] = hash_fn({key: report[key] for key in ("sections", "claim_ids", "citations", "evidence_epoch")})


class FixtureJudge:
    def __init__(self):
        self.support_status = {}
        self.scores = {criterion: 3 for criterion in CRITERIA}
        self.findings = []
        self.sufficiency_response = None
        self.response = None
        self.error = None
        self.calls = []

    async def __call__(self, *, prompt, payload, schema):
        self.calls.append(deepcopy(payload))
        if self.error:
            raise self.error
        if self.response is not None:
            return deepcopy(self.response)
        if "support_status" in schema["properties"]:
            claim = payload["claim"]
            # Deterministic fixture oracle; no claim of natural-language accuracy.
            status = self.support_status.get(claim["claim_id"],
                "supported" if all(claim["text"] == ex["evidence"]["quote"] for ex in payload["excerpts"]) else "unsupported")
            return {"support_status": status, "reason": "Synthetic fixture entailment verdict",
                    "supported_evidence_ids": claim["evidence_ids"] if status == "supported" else []}
        if "semantic_pass" in schema["properties"]:
            return deepcopy(self.sufficiency_response) if self.sufficiency_response else {
                "semantic_pass": True, "reason": "Both fixture questions are answered within their limited scope", "gaps": []}
        return {"criterion_scores": deepcopy(self.scores), "findings": deepcopy(self.findings)}


def services(case, *, verifier_judge=None, sufficiency_judge=None, report_judge=None,
             timeout_seconds=1):
    stored = {}

    async def excerpt_loader(source, evidence):
        body = case["bodies"].get(source["source_id"])
        if body is None or quote_hash(body) != source["sha256"]:
            raise ValueError("Fixture source file missing/hash mismatch")
        return body

    async def save(payload, *, key):
        stored[key] = deepcopy(payload)
        return {"relative_path": key, "sha256": hash_fn(payload), "media_type": "application/json", "schema_version": "1.0"}

    valid_receipt = hash_fn(record(factory("RetrievalReceipt",
        case["perspective_results"]["technology_maturity"]["retrieval_receipts"][0])))

    async def receipt_validator(receipt):
        return hash_fn(receipt) == valid_receipt

    verifier = ClaimSupportVerifier(judge=verifier_judge or FixtureJudge(), excerpt_loader=excerpt_loader,
                                   timeout_seconds=timeout_seconds)
    assessor = SufficiencyAssessor(verifier=verifier, judge=sufficiency_judge or FixtureJudge(),
                                  save_claim_support=save, receipt_validator=receipt_validator,
                                  company_id="alpha", corpus_version="corpus-1", index_version="index-1",
                                  timeout_seconds=timeout_seconds)
    evaluator = ReportEvaluator(verifier=verifier, judge=report_judge or FixtureJudge(), company_id="alpha", judge_model="fixture-judge", timeout_seconds=timeout_seconds)
    return verifier, assessor, evaluator, stored


def load_cases(test_file):
    return Path(test_file).resolve().parents[3] / "fixtures" / "v2" / "hyojin" / "cases.py"
