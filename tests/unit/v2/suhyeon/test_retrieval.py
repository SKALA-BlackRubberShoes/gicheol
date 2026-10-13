import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace
from dataclasses import replace

import pytest
from qdrant_client import QdrantClient, models

from main.agents.supervisor.contracts import ArtifactRef, Domain, Source, canonical_hash
from main.rag.neutral.chunking import Chunk
from main.rag.neutral.retrieval import (
    ArtifactIntegrityError, CorpusDomainUnavailableError, IndexMismatchError, NeutralRetriever, SourceInaccessibleError,
)


class Store:
    def __init__(self):
        self.values, self.inaccessible, self.unavailable = {}, set(), set()

    async def save(self, payload, *, key, media_type="application/json"):
        ref = ArtifactRef(relative_path=key, sha256=canonical_hash(payload), media_type=media_type)
        self.values[key] = payload
        return ref

    async def verify(self, ref):
        if ref.relative_path in self.unavailable:
            raise SourceInaccessibleError("source acquisition unavailable")
        if ref.relative_path in self.inaccessible:
            raise FileNotFoundError(ref.relative_path)
        assert canonical_hash(self.values[ref.relative_path]) == ref.sha256

    async def load(self, ref):
        await self.verify(ref)
        return self.values[ref.relative_path]


class Embedder:
    model_id, revision, dimension, normalize = "test-dense", "a" * 40, 3, True
    calls = 0
    async def embed_documents(self, texts):
        return [[1.0, 0.0, 0.0] for _ in texts]
    async def embed_query(self, text):
        self.calls += 1
        return [1.0, 0.0, 0.0]


async def setup(*, future=False, foreign=False, event_future=False, aliases=None, index_revision="fixture", client=None, forged_quote=False):
    store = Store()
    content = await store.save({"pages": [{"page_number": i + 1, "text": f"actual PDF quote {i}"} for i in range(2)],
                               "body_verified": True, "company_ids": ["other"] if foreign else ["14"],
                               "domain": Domain.PHYSICAL_AI_ROBOTICS.value, "original_pdf_sha256": "f" * 64}, key="corpus/original.json")
    source = Source(source_id="doc1", original_file_id="file.pdf", title="Actual PDF", publisher="Lab",
                    publisher_group="lab", source_type="paper", published_at=None,
                    retrieved_at=datetime.now(timezone.utc), corporate_controlled=False,
                    sha256=content.sha256, content_ref=content)
    chunks = []
    for i in range(2):
        chunks.append(Chunk(chunk_id=f"chunk{i}", document_id="doc1", source_id="doc1",
                      company_ids=("other",) if foreign else ("14",), domain=Domain.PHYSICAL_AI_ROBOTICS,
                      publisher_group="lab", page_number=i + 1, text=f"actual PDF quote {i}",
                      char_start=0, char_end=18, token_count=5, original_sha256="f" * 64,
                      published_at=date(2027, 1, 1) if future else date(2024, 1, 1),
                      event_date=date(2027, 1, 1) if event_future else None))
    if forged_quote:
        chunks[0] = replace(chunks[0], text="fabricated PDF quote")
    corpus = SimpleNamespace(sources={"doc1": source}, chunks=tuple(chunks), corpus_hash="c" * 64, corpus_version="1.0")
    embedding = Embedder()
    retriever = NeutralRetriever(corpus=corpus, embedding=embedding, store=store,
                                client=client or QdrantClient(":memory:"), index_revision=index_revision, company_aliases=aliases)
    await retriever.initialize()
    return retriever, store, embedding


def retrieve(retriever, **kwargs):
    return asyncio.run(retriever.retrieve("actual query", company_id=kwargs.pop("company_id", "14"),
                                         domain=kwargs.pop("domain", Domain.PHYSICAL_AI_ROBOTICS),
                                         as_of_date=date(2026, 10, 13), **kwargs))


def test_real_local_qdrant_success_receipt_and_persistent_shared_dtos():
    retriever, store, _ = asyncio.run(setup())
    result = retrieve(retriever)
    assert result.receipt.outcome == "success"
    assert [h.rank for h in result.receipt.ranked_hits] == [1, 2]
    assert {e.page_number for e in result.evidence} == {1, 2}
    assert len(result.sources) == 1
    assert result.receipt.queries_hash == canonical_hash(["actual query"])
    assert result.receipt.query_id in retriever.receipt_refs
    assert all(e.evidence_id in retriever.evidence_refs for e in result.evidence)
    assert retriever.collection_name == "neutral_v2_suhyeon_fixture"
    assert store.values[retriever.receipt_refs[result.receipt.query_id].relative_path] == result.receipt.model_dump(mode="json")


@pytest.mark.parametrize("options", [{"future": True}, {"foreign": True}, {"event_future": True}])
def test_company_publication_and_event_as_of_filters(options):
    retriever, _, _ = asyncio.run(setup(**options))
    assert retrieve(retriever).receipt.outcome == "empty"


def test_source_exclusions_and_unknown_company_empty():
    retriever, _, _ = asyncio.run(setup())
    assert retrieve(retriever, excluded_source_ids=["doc1"]).receipt.outcome == "empty"
    assert retrieve(retriever, company_id="unregistered").receipt.outcome == "empty"


def test_explicit_company_alias_ownership():
    retriever, _, _ = asyncio.run(setup(aliases={"RLWRLD": "14"}))
    result = retrieve(retriever, company_id="RLWRLD")
    assert {e.company_id for e in result.evidence} == {"14"}
    assert result.receipt.filters["company_alias"] == "RLWRLD"


def test_source_inaccessible_receipt_never_returns_unverified_quote():
    retriever, store, _ = asyncio.run(setup())
    store.unavailable.add(retriever.sources["doc1"].content_ref.relative_path)
    result = retrieve(retriever)
    assert result.receipt.outcome == "source_inaccessible"
    assert result.evidence == []
    assert result.receipt.ranked_hits[0].rank == 1


@pytest.mark.parametrize("mutation", ["model", "count", "payload"])
def test_stale_or_tampered_index_is_rejected_before_returning_evidence(mutation):
    retriever, _, embedding = asyncio.run(setup())
    if mutation == "model":
        retriever.client.update_collection(retriever.collection_name, metadata={"wrong": True})
    else:
        points, _ = retriever.client.scroll(retriever.collection_name)
        if mutation == "count":
            retriever.client.delete(retriever.collection_name, points_selector=models.PointIdsList(points=[points[0].id]))
        else:
            retriever.client.set_payload(retriever.collection_name, payload={"text": "forged quote"}, points=[points[0].id])
    with pytest.raises(IndexMismatchError) as error:
        retrieve(retriever)
    assert error.value.receipt.outcome == "error"
    if mutation != "payload":
        assert embedding.calls == 0


def test_new_revision_does_not_overwrite_existing_collection():
    client = QdrantClient(":memory:")
    client.create_collection("legacy_openai", vectors_config=models.VectorParams(size=512, distance=models.Distance.COSINE))
    a, _, _ = asyncio.run(setup(client=client, index_revision="first"))
    b, _, _ = asyncio.run(setup(client=client, index_revision="second"))
    assert {c.name for c in client.get_collections().collections} == {"legacy_openai", a.collection_name, b.collection_name}


def test_unprepared_domain_rejected_with_error_receipt():
    retriever, _, _ = asyncio.run(setup())
    with pytest.raises(CorpusDomainUnavailableError) as error:
        retrieve(retriever, domain=Domain.ENERGY)
    assert error.value.receipt.outcome == "error"


def test_embedding_error_is_receipted_and_not_empty():
    retriever, _, embedding = asyncio.run(setup())
    async def fail(text):
        raise OSError("provider offline")
    embedding.embed_query = fail
    assert retrieve(retriever).receipt.outcome == "error"


def test_top_k_contract_rejects_silent_expansion():
    retriever, _, _ = asyncio.run(setup())
    with pytest.raises(ValueError):
        retrieve(retriever, top_k=6)


def test_chunk_quote_compared_with_persisted_original_page():
    retriever, _, _ = asyncio.run(setup(forged_quote=True))
    with pytest.raises(IndexMismatchError, match="acquired original PDF page"):
        retrieve(retriever)


def test_operational_retrieval_time_is_excluded_from_index_identity():
    retriever, store, _ = asyncio.run(setup())
    source = retriever.sources["doc1"].model_copy(update={"retrieved_at": datetime(2025, 1, 1, tzinfo=timezone.utc)})
    corpus = SimpleNamespace(sources={"doc1": source}, chunks=retriever.corpus.chunks,
                             corpus_hash=retriever.corpus.corpus_hash, corpus_version="1.0")
    next_adapter = NeutralRetriever(corpus=corpus, embedding=Embedder(), store=store)
    assert next_adapter.index_hash == retriever.index_hash


@pytest.mark.parametrize("kind", ["missing", "corrupt"])
def test_persisted_artifact_loss_or_corruption_is_permanent(kind):
    retriever, store, _ = asyncio.run(setup())
    key = retriever.sources["doc1"].content_ref.relative_path
    if kind == "missing":
        store.inaccessible.add(key)
    else:
        store.values[key]["pages"][0]["text"] = "tampered bytes"
    with pytest.raises(ArtifactIntegrityError) as error:
        retrieve(retriever)
    assert error.value.receipt.outcome == "error"
