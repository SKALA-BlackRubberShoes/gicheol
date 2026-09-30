"""Qdrant 컬렉션 재사용 시 CSV ID와 거리 방식의 무결성을 검증합니다."""

import csv
from uuid import NAMESPACE_URL, uuid5

import pytest
from qdrant_client import QdrantClient, models

from main.paths import DEFAULT_CSV_PATH
from main.rag.company import BaseRAG, RAGStoreError
from main.rag.company import baseRAG as br


class FakeEmbeddings:
    model_name = "text-embedding-3-small"
    dimensions = 3

    def __init__(self):
        self.document_calls = 0

    def embed_documents(self, texts):
        self.document_calls += 1
        return [[1.0, 0.0, 0.0] for _ in texts]

    def embed_query(self, text):
        return [1.0, 0.0, 0.0]


@pytest.fixture
def memory_client():
    client = QdrantClient(location=":memory:")
    yield client
    client.close()


def make_rag(client, path=DEFAULT_CSV_PATH):
    embed = FakeEmbeddings()
    rag = BaseRAG(path, dimensions=3, embeddings=embed)
    rag._client = client
    return rag, embed


def one_row_csv(path, company_id, *, company_name=None):
    with DEFAULT_CSV_PATH.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        headers = reader.fieldnames
        row = next(reader)
    row["company_id"] = company_id
    if company_name is not None:
        row["기업명"] = company_name
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=headers)
        writer.writeheader()
        writer.writerow(row)
    return path


def test_reuse_rejects_empty_index_left_by_failed_rebuild(memory_client, monkeypatch):
    rag, _ = make_rag(memory_client)
    assert rag.build_index() == 30

    def fail_upsert(**kwargs):
        raise ConnectionError("upsert failed")

    with monkeypatch.context() as patch:
        patch.setattr(memory_client, "upsert", fail_upsert)
        with pytest.raises(RAGStoreError):
            rag.build_index(rebuild=True)

    assert memory_client.count(br.COLLECTION_NAME, exact=True).count == 0
    restarted, _ = make_rag(memory_client)
    with pytest.raises(RAGStoreError, match="rebuild=True"):
        restarted.build_index()


def test_reuse_rejects_partially_missing_csv_index(memory_client):
    original, _ = make_rag(memory_client)
    assert original.build_index() == 30
    memory_client.delete(
        br.COLLECTION_NAME,
        points_selector=models.PointIdsList(
            points=[str(uuid5(NAMESPACE_URL, "basic-rag:17"))]
        ),
        wait=True,
    )
    assert memory_client.count(br.COLLECTION_NAME, exact=True).count == 29

    restarted, _ = make_rag(memory_client)
    with pytest.raises(RAGStoreError, match="rebuild=True"):
        restarted.build_index()


def test_reuse_checks_ids_without_requiring_equal_document_counts(memory_client, tmp_path):
    original, _ = make_rag(memory_client)
    assert original.build_index() == 30

    existing, existing_embed = make_rag(
        memory_client,
        one_row_csv(tmp_path / "existing.csv", "1", company_name="변경된 이름"),
    )
    assert existing.build_index() == 30
    assert existing_embed.document_calls == 0
    assert existing.get_company("1").company_name == "변경된 이름"

    missing, _ = make_rag(
        memory_client, one_row_csv(tmp_path / "missing.csv", "new-id")
    )
    with pytest.raises(RAGStoreError, match="rebuild=True"):
        missing.build_index()


def test_reuse_rejects_non_cosine_collection(memory_client):
    memory_client.create_collection(
        br.COLLECTION_NAME,
        vectors_config=models.VectorParams(size=3, distance=models.Distance.EUCLID),
    )
    rag, _ = make_rag(memory_client)
    with pytest.raises(ValueError, match="rebuild=True"):
        rag.build_index()
