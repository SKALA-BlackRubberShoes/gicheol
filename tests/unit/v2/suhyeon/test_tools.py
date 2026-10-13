"""Original-body evidence resolution, independent of LLM output."""
import asyncio

import pytest

from main.agents.common.evidence import verify_neutral_quote
from main.agents.supervisor.contracts import Domain, Evidence
from main.rag.neutral.tools import NeutralResearchTools, StoredEvidenceResolver, verify_acquired_evidence
from tests.fixtures.v2.suhyeon.store import DOMAIN, VerifiedJsonStore, stored_source


def test_resolver_survives_reconstruction_with_persisted_registry(tmp_path):
    async def scenario():
        store = VerifiedJsonStore(tmp_path)
        source, evidence = await stored_source(store)
        resolver = StoredEvidenceResolver(store=store, company_id="acme", domain=DOMAIN)
        await resolver.record([source], [evidence])
        reconstructed = StoredEvidenceResolver(store=VerifiedJsonStore(tmp_path), company_id="acme", domain=DOMAIN,
                                                source_refs=resolver.source_refs, evidence_refs=resolver.evidence_refs)
        bundle = await reconstructed.resolve([evidence.evidence_id])
        assert bundle.sources == [source] and bundle.evidence == [evidence]
    asyncio.run(scenario())


@pytest.mark.parametrize("change,error", [
    ({"company_id": "other"}, "EVIDENCE_COMPANY_OR_SOURCE_MISMATCH"),
    ({"quote": "Acme achieved perfect real-world performance", "quote_hash": None}, "QUOTE_NOT_IN_ORIGINAL_BODY"),
    ({"page_number": 2}, "QUOTE_NOT_IN_ORIGINAL_BODY"),
    ({"page_number": 9}, "PDF_PAGE_MISSING"),
    ({"page_number": None}, "PDF_EVIDENCE_REQUIRES_PAGE"),
])
def test_original_pdf_rejects_wrong_company_altered_quote_or_page(tmp_path, change, error):
    async def scenario():
        store = VerifiedJsonStore(tmp_path)
        source, evidence = await stored_source(store)
        changed = Evidence.model_validate(evidence.model_dump() | change)
        with pytest.raises(ValueError, match=error):
            await verify_acquired_evidence(store, changed, source, company_id="acme", domain=DOMAIN)
    asyncio.run(scenario())


@pytest.mark.parametrize("body,error", [
    ({"company_id": "other"}, "SOURCE_COMPANY_OR_DOMAIN_MISMATCH"),
    ({"domain": Domain.AGTECH}, "SOURCE_COMPANY_OR_DOMAIN_MISMATCH"),
    ({"body_verified": False}, "SNIPPET_IS_NOT_ACQUIRED_BODY"),
])
def test_original_body_must_be_acquired_and_scoped(tmp_path, body, error):
    async def scenario():
        store = VerifiedJsonStore(tmp_path)
        source, evidence = await stored_source(store, **body)
        with pytest.raises(ValueError, match=error):
            await verify_acquired_evidence(store, evidence, source, company_id="acme", domain=DOMAIN)
    asyncio.run(scenario())


def test_full_artifact_integrity_is_verified_even_outside_quoted_page(tmp_path):
    async def scenario():
        store = VerifiedJsonStore(tmp_path)
        source, evidence = await stored_source(store)
        raw = store.path(source.content_ref).read_text()
        store.path(source.content_ref).write_text(raw.replace("A separate page", "A tampered page"))
        with pytest.raises(ValueError, match="ARTIFACT_BYTES_HASH_MISMATCH"):
            await verify_acquired_evidence(store, evidence, source, company_id="acme", domain=DOMAIN)
    asyncio.run(scenario())


@pytest.mark.parametrize("chunk_id,error", [("unknown", "WEB_LOCATION_MISSING"),
                                            ("source-acme:1-10", "WEB_LOCATION_MISMATCH"),
                                            ("source-acme:-1-999", "WEB_LOCATION_MISSING")])
def test_web_evidence_requires_exact_body_character_span(tmp_path, chunk_id, error):
    async def scenario():
        store = VerifiedJsonStore(tmp_path)
        source, evidence = await stored_source(store, pdf=False)
        changed = evidence.model_copy(update={"chunk_id": chunk_id})
        with pytest.raises(ValueError, match=error):
            await verify_acquired_evidence(store, changed, source, company_id="acme", domain=DOMAIN)
    asyncio.run(scenario())


def test_resolver_rejects_unknown_or_duplicate_ids_and_revisions(tmp_path):
    async def scenario():
        store = VerifiedJsonStore(tmp_path)
        source, evidence = await stored_source(store)
        resolver = StoredEvidenceResolver(store=store, company_id="acme", domain=DOMAIN)
        await resolver.record([source], [evidence])
        with pytest.raises(ValueError, match="Duplicate evidence"):
            await resolver.resolve([evidence.evidence_id, evidence.evidence_id])
        with pytest.raises(ValueError, match="UNKNOWN_EVIDENCE_ID"):
            await resolver.resolve(["missing"])
        changed = source.model_copy(update={"publisher_group": "different-owner"})
        with pytest.raises(ValueError, match="SOURCE_ID_CONTENT_CHANGED"):
            await resolver.record([changed], [evidence])
    asyncio.run(scenario())


def test_quote_helper_rejects_wrong_hash_and_selected_location(tmp_path):
    async def scenario():
        store = VerifiedJsonStore(tmp_path)
        source, evidence = await stored_source(store)
        with pytest.raises(ValueError, match="EVIDENCE_PAGE_MISMATCH"):
            verify_neutral_quote(evidence, source, text=evidence.quote, company_id="acme", page_number=2)
        with pytest.raises(ValueError, match="EVIDENCE_CHUNK_MISMATCH"):
            verify_neutral_quote(evidence, source, text=evidence.quote, company_id="acme", page_number=1, chunk_id="different")
        with pytest.raises(ValueError, match="SOURCE_CONTENT_HASH_MISMATCH"):
            verify_neutral_quote(evidence, source.model_copy(update={"sha256": "a" * 64}),
                                 text=evidence.quote, company_id="acme", page_number=1)
    asyncio.run(scenario())


def test_facade_exposes_only_research_and_requires_configured_search():
    async def scenario():
        tools = NeutralResearchTools(retriever=object())
        assert not hasattr(tools, "writer") and not hasattr(tools, "evaluator") and not hasattr(tools, "market")
        with pytest.raises(RuntimeError, match="SEARCH_PROVIDER_NOT_CONFIGURED"):
            await tools.search("query", company_id="acme", domain=DOMAIN, as_of_date=None)
        with pytest.raises(RuntimeError, match="SEARCH_PROVIDER_NOT_CONFIGURED"):
            await tools.fetch(None)
    asyncio.run(scenario())


def test_resolver_uses_recorded_refs_and_rejects_swapped_identity_or_deleted_body(tmp_path):
    async def scenario():
        store = VerifiedJsonStore(tmp_path)
        source, evidence = await stored_source(store)
        resolver = StoredEvidenceResolver(store=store, company_id="acme", domain=DOMAIN)
        await resolver.record([source], [evidence])
        with pytest.raises(ValueError, match="UNKNOWN_EVIDENCE_ID"):
            await resolver.resolve(["../../outside-original.json"])
        resolver.evidence_refs["wrong-registry-id"] = resolver.evidence_refs[evidence.evidence_id]
        with pytest.raises(ValueError, match="EVIDENCE_ID_MISMATCH"):
            await resolver.resolve(["wrong-registry-id"])
        store.path(source.content_ref).unlink()
        with pytest.raises(FileNotFoundError):
            await resolver.resolve([evidence.evidence_id])
    asyncio.run(scenario())


def test_same_web_body_recorded_again_can_change_only_operational_timestamp(tmp_path):
    from datetime import timedelta
    async def scenario():
        store = VerifiedJsonStore(tmp_path)
        source, evidence = await stored_source(store, pdf=False)
        resolver = StoredEvidenceResolver(store=store, company_id="acme", domain=DOMAIN)
        await resolver.record([source], [evidence])
        newer = source.model_copy(update={"retrieved_at": source.retrieved_at + timedelta(minutes=1)})
        await resolver.record([newer], [evidence])
        assert (await resolver.resolve([evidence.evidence_id])).sources == [source]
    asyncio.run(scenario())
