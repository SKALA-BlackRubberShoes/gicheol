"""Independent claim verification against resolved original text."""

from __future__ import annotations

from collections.abc import Callable, Sequence
import re
from typing import Any

from .rubrics import (
    CLAIM_FIELDS, CLAIM_JUDGE_SCHEMA, EVIDENCE_FIELDS, VERIFIER_VERSION,
    QualityInputError, call_async, digest, index, parse_date, prompt, quote_hash,
    record, records, validate_response,
)


class ClaimSupportVerifier:
    """The resolver must load hash-checked full-text pages/chunks, never snippets.

    ``contract_factory(name, payload)`` validates with the common DTO model.
    ``hash_fn`` is the shared canonical hash implementation. No fallback DTOs
    or model configuration are created in this module.
    """

    def __init__(self, *, judge: Callable, excerpt_loader: Callable,
                 contract_factory: Callable, hash_fn: Callable,
                 timeout_seconds: float = 30, verifier_version=VERIFIER_VERSION):
        self.judge = judge
        self.excerpt_loader = excerpt_loader
        self.contract_factory = contract_factory
        self.hash_fn = hash_fn
        self.timeout_seconds = timeout_seconds
        self.verifier_version = verifier_version

    async def prepare(self, claim: Any, *, evidence: Sequence[Any], sources: Sequence[Any],
                      company_id: str, as_of_date: str) -> dict[str, Any]:
        """Resolve and validate original excerpts for the verifier/report Judge."""
        claim = record(claim)
        evidence_by_id = index(records(evidence), "evidence_id")
        source_by_id = index(records(sources), "source_id")
        cutoff = parse_date(as_of_date)
        ids = claim.get("evidence_ids")
        if not isinstance(ids, list) or not ids or len(ids) != len(set(ids)):
            raise QualityInputError("Claim needs distinct evidence IDs")
        if claim.get("kind") not in {"fact", "inference"}:
            raise QualityInputError("Unknown questions do not establish supported claims")
        if not isinstance(claim.get("text"), str) or not claim["text"].strip():
            raise QualityInputError("Empty claim text")
        if claim["kind"] == "inference" and not claim.get("limitations"):
            raise QualityInputError("Inference is missing explicit conditions/limitations")
        excerpts = []
        for eid in ids:
            ev = evidence_by_id.get(eid)
            source = source_by_id.get(ev.get("source_id")) if ev else None
            if not ev or not source:
                raise QualityInputError("Claim references missing evidence/source")
            # Adjacent text lets the Judge see qualifications omitted by the quote.
            excerpts.append(await self.resolve_evidence(ev, source, company_id=company_id,
                                                        as_of_date=cutoff.isoformat()))
        return {"claim": claim, "excerpts": excerpts, "company_id": company_id,
                "as_of_date": cutoff.isoformat()}

    async def resolve_evidence(self, evidence: Any, source: Any, *, company_id: str,
                               as_of_date: str) -> dict[str, Any]:
        """Validate and resolve an excerpt, including contrary/unclaimed evidence."""
        ev, source = record(evidence), record(source)
        cutoff = parse_date(as_of_date)
        if ev.get("source_id") != source.get("source_id") or ev.get("company_id") != company_id:
            raise QualityInputError("Evidence belongs to a different source or company")
        if (source.get("source_type") not in {"company", "paper", "institution", "news", "dataset"}
                or not source.get("content_ref")
                or source["content_ref"].get("sha256") != source.get("sha256")
                or not isinstance(source.get("publisher_group"), str)
                or not source["publisher_group"].strip()
                or type(source.get("corporate_controlled")) is not bool
                or not re.fullmatch(r"[a-f0-9]{64}", source.get("sha256", ""))):
            raise QualityInputError("Source lacks verified full-text provenance")
        quote = ev.get("quote")
        if not isinstance(quote, str) or not quote.strip() or ev.get("quote_hash") != quote_hash(quote):
            raise QualityInputError("Evidence quote hash mismatch or empty quote")
        if not isinstance(ev.get("chunk_id"), str) or not ev["chunk_id"].strip():
            raise QualityInputError("Evidence is missing its original text locator")
        page = ev.get("page_number")
        if page is not None and (type(page) is not int or page < 1):
            raise QualityInputError("Invalid PDF page number")
        for field, value in (("event_date", ev.get("event_date")),
                             ("published_at", source.get("published_at"))):
            if value is not None and parse_date(value) > cutoff:
                raise QualityInputError(f"{field} is after the assessment cutoff")
        original = await call_async(self.excerpt_loader, source, ev, timeout_seconds=self.timeout_seconds)
        if not isinstance(original, str) or quote not in original:
            raise QualityInputError("Quote is absent from resolved original text")
        return {"evidence": ev, "source": source, "original_text": original}

    async def verify(self, claim: Any, *, evidence: Sequence[Any], sources: Sequence[Any],
                     company_id: str, as_of_date: str):
        claim = record(claim)
        evidence = records(evidence)
        sources = records(sources)
        evidence_by_id = index(evidence, "evidence_id")
        # Malformed shared input is an operation error, never an approved support.
        claim_digest = digest(claim, CLAIM_FIELDS, self.hash_fn)
        hashes = {eid: digest(evidence_by_id[eid], EVIDENCE_FIELDS, self.hash_fn)
                  for eid in claim["evidence_ids"] if eid in evidence_by_id}
        status, reason = "unverified", "Verification did not complete"
        try:
            payload = await self.prepare(claim, evidence=evidence, sources=sources,
                                         company_id=company_id, as_of_date=as_of_date)
            response = await call_async(
                self.judge, prompt=prompt("claim_support"), payload=payload,
                schema=CLAIM_JUDGE_SCHEMA, timeout_seconds=self.timeout_seconds,
            )
            response = validate_response(response, CLAIM_JUDGE_SCHEMA)
            supported_ids = set(response["supported_evidence_ids"])
            if not supported_ids <= set(claim["evidence_ids"]):
                raise QualityInputError("Verifier invented an evidence ID")
            status, reason = response["support_status"], response["reason"]
            if status == "supported" and supported_ids != set(claim["evidence_ids"]):
                status, reason = "partial", "Not every cited excerpt supports the claim: " + reason
        except Exception as exc:
            # Cancellation/KeyboardInterrupt propagate; technical failures never pass.
            status, reason = "unverified", f"Verification failed ({type(exc).__name__}): {exc}"[:2000]
        return self.contract_factory("ClaimSupport", {
            "claim_id": claim["claim_id"], "claim_hash": claim_digest,
            "evidence_hashes": hashes, "support_status": status, "reason": reason,
            "verifier_version": self.verifier_version,
        })
