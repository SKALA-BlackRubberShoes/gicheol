"""Explicit deterministic research fixtures; never evidence for a real company.

The JSON store verifies actual saved bytes. AT08 uses a generated, actual PDF
and real local Qdrant; only dense vectors and structured model replies are fake.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path

from main.agents.supervisor.contracts import (
    ArtifactRef, Claim, Domain, Evidence, Gap, GapKind, Perspective, RankedHit,
    ResearchRequest, RetrievalReceipt, RetrievalResult, Severity, Source,
    canonical_hash, canonical_json,
)

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
DOMAIN = Domain.PHYSICAL_AI_ROBOTICS
AS_OF = date(2026, 1, 1)


class VerifiedJsonStore:
    """Fixture storage with real file-byte hashes and safe paths."""
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.saved_keys = []

    def path(self, ref):
        value = self.root / ref.relative_path
        if not value.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("UNSAFE_ARTIFACT_PATH")
        return value

    async def save(self, payload, *, key, media_type="application/json"):
        ref = ArtifactRef(relative_path=key, sha256="0" * 64, media_type=media_type)
        path = self.path(ref)
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = canonical_json(payload).encode("utf-8")
        path.write_bytes(raw)
        self.saved_keys.append(key)
        return ref.model_copy(update={"sha256": hashlib.sha256(raw).hexdigest()})

    async def verify(self, ref):
        raw = self.path(ref).read_bytes()
        if hashlib.sha256(raw).hexdigest() != ref.sha256:
            raise ValueError("ARTIFACT_BYTES_HASH_MISMATCH")

    async def load(self, ref):
        await self.verify(ref)
        value = json.loads(self.path(ref).read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("JSON artifact must be an object")
        return value


class FixtureEmbeddings:
    """Deterministic unit vectors, explicitly not a production embedding model."""
    model_id = "deterministic-fixture"
    revision = "f" * 40
    dimension = 3
    normalize = True

    async def embed_documents(self, texts):
        return [[1.0, 0.0, 0.0] for _ in texts]

    async def embed_query(self, text):
        return [1.0, 0.0, 0.0]


class FixtureLLM:
    """Common Claim/Gap responses; no supported verdict or downstream calls."""
    def __init__(self, *, mode="valid", cross_view_gap=False):
        self.calls = []
        self.mode, self.cross_view_gap = mode, cross_view_gap

    async def generate(self, *, model, system_prompt, payload, response_model):
        self.calls.append((model, system_prompt, payload, response_model))
        eid = payload["evidence"][0]["evidence_id"]
        if self.mode == "invalid_citation":
            eid = "made-up-evidence"
        perspective = Perspective.MARKET if self.mode == "wrong_perspective" else Perspective.TECHNOLOGY_MATURITY
        kind = "unknown" if self.mode == "unknown_answer" else "fact"
        claim = Claim(claim_id="claim-tech", perspective=perspective,
                      text="The Acme fixture describes a sensor fusion model and a bounded laboratory trial.",
                      kind=kind, evidence_ids=[eid], limitations=["Deterministic test fixture only."])
        gaps = [Gap(gap_id="market-gap", perspective=Perspective.MARKET,
                    kind=GapKind.MISSING_EVIDENCE, question="What is customer demand?",
                    severity=Severity.WARNING, query_hints=["Acme customer demand"])] if self.cross_view_gap else []
        response = {
            "claims": [claim], "answered_questions": {
                "tech.principle": [claim.claim_id], "tech.validation": [claim.claim_id]},
            "gaps": gaps, "limitations": ["Test fixture output."]}
        if self.mode == "wrong_question":
            response["answered_questions"] = {"market.demand": [claim.claim_id]}
        if self.mode == "self_support":
            response["support_status"] = "supported"
        return response_model.model_validate(response)


def request(**changes):
    payload = dict(task_id="task-tech", company_id="acme", perspective=Perspective.TECHNOLOGY_MATURITY,
                   as_of_date=AS_OF, attempt=1, corpus_version="fixture-v1")
    return ResearchRequest.model_validate(payload | changes)


def write_pdf(path, texts):
    from reportlab.pdfgen import canvas
    path = Path(path)
    output = canvas.Canvas(str(path), invariant=1)
    for text in texts:
        output.drawString(40, 740, text)
        output.showPage()
    output.save()
    return path


async def stored_source(store, *, pdf=True, body_verified=True, company_id="acme", domain=DOMAIN):
    pages = [{"page_number": 1, "text": "Acme uses sensor fusion. Validation used ten laboratory trials."},
             {"page_number": 2, "text": "A separate page does not contain the first quotation."}]
    body = dict(company_ids=[company_id], domain=domain.value, body_verified=body_verified)
    body.update({"pages": pages} if pdf else {"text": pages[0]["text"], "pages": None})
    ref = await store.save(body, key="original/source.json")
    source = Source(source_id="source-acme", original_file_id="acme.pdf" if pdf else None,
                    canonical_url=None if pdf else "https://example.test/acme",
                    title="Acme fixture trial", publisher="Fixture Institute",
                    publisher_group="fixture-institute", source_type="institution", published_at=None,
                    retrieved_at=NOW, corporate_controlled=False, sha256=ref.sha256, content_ref=ref)
    quote = pages[0]["text"]
    item = Evidence(evidence_id="evidence-acme", source_id=source.source_id, company_id="acme",
                    chunk_id="acme-page-one" if pdf else f"source-acme:0-{len(quote)}",
                    page_number=1 if pdf else None, quote=quote)
    return source, item


def retrieval_result(*, source=None, evidence=None, outcome="success", query="fixture query"):
    hits = [RankedHit(source_id=source.source_id, chunk_id=evidence.chunk_id, rank=1, score=1.0)] if source else []
    receipt = RetrievalReceipt(query_id="receipt-" + canonical_hash(query)[:16], query_text=query,
                               queries_hash=canonical_hash([query]), provider="deterministic-fixture",
                               corpus_version="fixture-v1", index_version="fixture-index",
                               filters={"company_id": "acme", "domain": DOMAIN.value,
                                        "as_of_date": AS_OF.isoformat()},
                               ranked_hits=hits, outcome=outcome, ts=NOW)
    return RetrievalResult(sources=[source] if source else [], evidence=[evidence] if evidence else [], receipt=receipt)


class FixtureTools:
    def __init__(self, result, *, receipt_overrides=None):
        self.result, self.calls = result, []
        self.receipt_overrides = receipt_overrides or {}

    async def retrieve(self, query, **kwargs):
        self.calls.append((query, kwargs))
        receipt_payload = self.result.receipt.model_dump() | {
            "query_id": "receipt-" + canonical_hash(query)[:16],
            "query_text": query, "queries_hash": canonical_hash([query]),
            "corpus_version": "fixture-v1",
            "filters": {"company_id": kwargs["company_id"], "domain": Domain(kwargs["domain"]).value,
                        "as_of_date": kwargs["as_of_date"].isoformat()},
        }
        receipt = RetrievalReceipt.model_validate(receipt_payload | self.receipt_overrides)
        return RetrievalResult(sources=self.result.sources, evidence=self.result.evidence, receipt=receipt)

    async def search(self, *args, **kwargs):
        raise AssertionError("Web search was not requested in this fixture")

    async def fetch(self, *args, **kwargs):
        raise AssertionError("Fetch was not requested in this fixture")
