"""AT08: actual PDF limits and immutable local vector index integration.

Fixtures explicitly fake vectors and model responses. PDF bytes, parsing,
Qdrant persistence/search, quote locations, and artifact hashes are real.
"""
import asyncio
import base64
import hashlib

import pytest
from qdrant_client import QdrantClient

from main.agents.perspectives.technology import TechnologyWorker
from main.rag.neutral.ingestion import CorpusIngestionError, DocumentInput, ingest_documents
from main.rag.neutral.retrieval import IndexMismatchError, NeutralRetriever
from main.rag.neutral.tools import NeutralResearchTools
from tests.fixtures.v2.suhyeon.store import (
    AS_OF, DOMAIN, NOW, FixtureEmbeddings, FixtureLLM, VerifiedJsonStore, request, write_pdf,
)


def document(path, **changes):
    return DocumentInput(**(dict(path=path, source_id="acme-pdf", company_ids=("acme",),
                                company_names=("Acme",), domain=DOMAIN,
                                publisher_group="fixture-institute", title="Acme actual PDF fixture",
                                publisher="Fixture Institute", source_type="institution",
                                corporate_controlled=False, retrieved_at=NOW) | changes))


def test_at08_actual_201_pages_fail_before_any_artifact_or_index(tmp_path):
    async def scenario():
        pdf = write_pdf(tmp_path / "over-budget.pdf", [f"Acme trial page {i}" for i in range(201)])
        store = VerifiedJsonStore(tmp_path / "store")
        with pytest.raises(CorpusIngestionError, match="200-page"):
            await ingest_documents([document(pdf)], store=store, corpus_version="fixture-v1")
        assert store.saved_keys == []
    asyncio.run(scenario())


def test_at08_actual_200_pages_produce_technology_pdf_rag_receipts(tmp_path):
    async def scenario():
        pdf = write_pdf(tmp_path / "in-budget.pdf", [f"Acme sensor fusion model validation laboratory trial page {i}" for i in range(200)])
        store = VerifiedJsonStore(tmp_path / "store")
        corpus = await ingest_documents([document(pdf)], store=store, corpus_version="fixture-v1",
                                        company_aliases={"acme": ["Acme"]})
        assert corpus.total_pages == 200
        client = QdrantClient(path=str(tmp_path / "qdrant"))
        try:
            retriever = NeutralRetriever(corpus=corpus, embedding=FixtureEmbeddings(), store=store, client=client)
            await retriever.initialize()
            llm = FixtureLLM(cross_view_gap=True)
            worker = TechnologyWorker(tools=NeutralResearchTools(retriever=retriever), store=store,
                                      llm=llm, model="explicit-fixture-model", company_id="acme",
                                      company_name="Acme", domain=DOMAIN, corpus_version=corpus.corpus_version)
            result = await worker(request())
            assert result.claims and result.evidence_ids and len(llm.calls) == 1
            assert set(result.answered_questions) == {"tech.principle", "tech.validation"}
            assert all(r.outcome == "success" and r.ranked_hits for r in result.retrieval_receipts)
            assert all(r.index_version == retriever.index_revision for r in result.retrieval_receipts)
            assert any(g.perspective.value == "market" for g in result.gaps)
            bundle = await worker.resolver.resolve(result.evidence_ids)
            assert len(bundle.sources) == 1
            for evidence in bundle.evidence:
                page = corpus.pages[evidence.page_number - 1]
                chunk = next(c for c in corpus.chunks if c.chunk_id == evidence.chunk_id)
                assert page.text[chunk.char_start:chunk.char_end] == evidence.quote
                assert hashlib.sha256(evidence.quote.encode()).hexdigest() == evidence.quote_hash
            acquired = await store.load(bundle.sources[0].content_ref)
            original = base64.b64decode(acquired["original_pdf_base64"], validate=True)
            assert original == pdf.read_bytes()
            assert hashlib.sha256(original).hexdigest() == acquired["original_pdf_sha256"]
            assert len(acquired["pages"]) == 200
        finally:
            client.close()
    asyncio.run(scenario())


def test_at08_changed_embedding_revision_rejects_existing_collection(tmp_path):
    async def scenario():
        pdf = write_pdf(tmp_path / "trial.pdf", ["Acme sensor fusion model validation laboratory trials"])
        store = VerifiedJsonStore(tmp_path / "store")
        corpus = await ingest_documents([document(pdf)], store=store, corpus_version="fixture-v1")
        client = QdrantClient(path=str(tmp_path / "qdrant"))
        try:
            original = NeutralRetriever(corpus=corpus, embedding=FixtureEmbeddings(), store=store,
                                        client=client, index_revision="fixed-fixture-revision")
            await original.initialize()
            incompatible = FixtureEmbeddings()
            incompatible.revision = "a" * 40
            changed = NeutralRetriever(corpus=corpus, embedding=incompatible, store=store,
                                       client=client, index_revision="fixed-fixture-revision")
            with pytest.raises(IndexMismatchError):
                await changed.initialize()
            intact = await original.retrieve("Acme trial", company_id="acme", domain=DOMAIN, as_of_date=AS_OF)
            assert intact.receipt.outcome == "success" and intact.evidence
        finally:
            client.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("damage", ["tampered_bytes", "missing_file"])
def test_acquired_pdf_artifact_integrity_failure_cannot_be_retried_as_empty_or_source_inaccessible(tmp_path, damage):
    async def scenario():
        pdf = write_pdf(tmp_path / "trial.pdf", ["Acme sensor fusion model validation laboratory trials"])
        store = VerifiedJsonStore(tmp_path / "store")
        corpus = await ingest_documents([document(pdf)], store=store, corpus_version="fixture-v1")
        client = QdrantClient(path=str(tmp_path / "qdrant"))
        try:
            retriever = NeutralRetriever(corpus=corpus, embedding=FixtureEmbeddings(), store=store, client=client)
            await retriever.initialize()
            body = store.path(corpus.sources["acme-pdf"].content_ref)
            if damage == "missing_file":
                body.unlink()
            else:
                body.write_bytes(body.read_bytes() + b"tampered")
            llm = FixtureLLM()
            worker = TechnologyWorker(tools=NeutralResearchTools(retriever=retriever), store=store,
                                      llm=llm, model="explicit-fixture-model", company_id="acme",
                                      company_name="Acme", domain=DOMAIN, corpus_version=corpus.corpus_version)
            result = await worker(request())
            assert result.outcome == "permanent_error"
            assert result.error.code.value == "ARTIFACT_INTEGRITY_ERROR" and result.error.retriable is False
            assert not llm.calls
            assert len(retriever.receipt_refs) == 1
            failed_receipt = await store.load(next(iter(retriever.receipt_refs.values())))
            assert failed_receipt["outcome"] == "error"
        finally:
            client.close()
    asyncio.run(scenario())
