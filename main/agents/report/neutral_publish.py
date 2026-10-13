"""Staging publication worker; storage and graph own the completed commit."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
import tempfile

from main.agents.supervisor import contracts as c
from .neutral_builder import validate_references
from .neutral_renderers import PdfLayoutError, render_neutral_pdf


class NeutralPublisher:
    def __init__(self, *, store: c.ArtifactStore, evidence_resolver: c.EvidenceResolver,
                 artifact_root: Path, font_path: str):
        self.store, self.evidence_resolver = store, evidence_resolver
        self.artifact_root = Path(artifact_root).resolve()
        self.font_path = font_path

    async def __call__(self, request: c.PublishRequest) -> c.PublishResult:
        request = c.PublishRequest.model_validate(request.model_dump(mode="json"))
        report = request.report
        if request.gate.rubric_version != request.evaluation.rubric_version:
            raise ValueError("publication gate and evaluation use different rubrics")
        if report.document_ref is not None:
            await self.store.verify(report.document_ref)
        await self.store.verify(request.gate.reason_ref)
        ids = sorted({item.evidence_id for item in report.citations})
        bundle = await self.evidence_resolver.resolve(ids)
        bundle = c.EvidenceBundle.model_validate(bundle.model_dump(mode="json"))
        if {e.evidence_id for e in bundle.evidence} != set(ids):
            raise ValueError("publication resolver did not return the exact cited evidence")
        claims = c.deduplicate_claims(request.eligibility.claims + [
            claim for result in request.perspective_results.values() for claim in result.claims])
        # Validate the referenced subset; unused approved evidence is not required.
        used_claims = [claim for claim in claims if claim.claim_id in report.claim_ids]
        by_claim = {claim.claim_id: claim for claim in used_claims}
        if set(report.claim_ids) != set(by_claim):
            raise ValueError("publication report uses unknown claims")
        for section in report.sections[:-1]:
            for paragraph in section.paragraphs:
                if paragraph.kind in {"fact", "inference"}:
                    if len(paragraph.claim_ids) != 1:
                        raise ValueError("publication claim provenance changed")
                    claim = by_claim[paragraph.claim_ids[0]]
                    if (paragraph.text, paragraph.kind, paragraph.evidence_ids) != (claim.text, claim.kind, claim.evidence_ids):
                        raise ValueError("publication report changed approved claim content")
        validate_references(report, bundle.sources, bundle.evidence)
        for source in bundle.sources:
            await self.store.verify(source.content_ref)
        staging = self.artifact_root / "staging"
        staging.mkdir(parents=True, exist_ok=True)
        if staging.resolve() != staging:
            raise ValueError("staging directory escapes the configured artifact root")
        # Unique candidate paths avoid two retries overwriting a previously validated
        # file. Rendering is blocking; the async worker runs it outside the event loop.
        # Cancellation may leave a staging candidate, but can never commit a manifest.
        with tempfile.TemporaryDirectory(prefix="neutral-render-", dir=staging) as temp:
            candidate = Path(temp) / f"report-{report.revision}.pdf"
            try:
                metrics = await asyncio.to_thread(render_neutral_pdf, report, candidate, font_path=self.font_path)
                digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
                destination = staging / f"report-{report.revision}-{digest}.pdf"
                if destination.is_symlink():
                    raise ValueError("staging PDF cannot be a symlink")
                candidate.replace(destination)
                ref = c.ArtifactRef(relative_path=destination.relative_to(self.artifact_root).as_posix(),
                                    sha256=digest, media_type="application/pdf")
                await self.store.verify(ref)
            except PdfLayoutError as exc:
                # Diagnostics preserve the evaluated draft identity; failure is raised
                # for the graph's PDF_LAYOUT_FAILED handling, never returned as pass.
                await self.store.save({"code": "PDF_LAYOUT_FAILED", "report_hash": report.content_hash,
                    "report_revision": report.revision, "evidence_epoch": report.evidence_epoch,
                    "message": str(exc)[:2000]}, key=f"diagnostics/pdf-{report.revision}.json")
                raise
        return c.PublishResult(report_revision=report.revision, report_hash=report.content_hash,
            evidence_epoch=report.evidence_epoch, staging_pdf_ref=ref, final_manifest_ref=None, **metrics)
