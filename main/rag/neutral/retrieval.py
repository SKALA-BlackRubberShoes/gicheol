"""Isolated, immutable Qdrant PDF retrieval using the common V2 DTOs."""
from __future__ import annotations

import asyncio
import re
import uuid
from datetime import date, datetime, timezone
from typing import Any, Iterable

from main.agents.supervisor.contracts import (
    ArtifactRef, ArtifactStore, Domain, EmbeddingProvider, Evidence, RankedHit,
    RetrievalReceipt, RetrievalResult, Source, canonical_hash,
)
from main.rag.neutral.embeddings import validate_vectors


class IndexMismatchError(RuntimeError):
    """Permanent INDEX_MISMATCH, with the unsuccessful actual query receipt."""
    receipt: RetrievalReceipt | None = None


class CorpusDomainUnavailableError(RuntimeError):
    """A domain without actual prepared PDF chunks is unsupported."""
    receipt: RetrievalReceipt | None = None


class ArtifactIntegrityError(RuntimeError):
    """Persisted artifact loss or corruption, a permanent integrity failure."""
    receipt: RetrievalReceipt | None = None


class SourceInaccessibleError(RuntimeError):
    """An explicitly unavailable upstream source (not a lost saved artifact)."""


def _component(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", value) or value in {".", ".."}:
        raise ValueError("Artifact identifiers must be safe path components")
    return value


def _chunk_payload(chunk: Any, *, corpus_hash: str, index_hash: str) -> dict[str, Any]:
    return {
        "chunk_id": chunk.chunk_id, "source_id": chunk.source_id,
        "document_id": chunk.document_id, "company_ids": list(chunk.company_ids),
        "domain": chunk.domain.value, "publisher_group": chunk.publisher_group,
        "page_number": chunk.page_number, "text": chunk.text,
        "char_start": chunk.char_start, "char_end": chunk.char_end,
        "token_count": chunk.token_count,
        "published_at": chunk.published_at.isoformat() if chunk.published_at else None,
        "event_date": chunk.event_date.isoformat() if chunk.event_date else None,
        "original_sha256": chunk.original_sha256, "corpus_hash": corpus_hash,
        "index_hash": index_hash,
    }


class NeutralRetriever:
    """Common retrieval adapter; existing collections are never overwritten.

    Construct with a prepared ingestion.Corpus and an async EmbeddingProvider.
    Call initialize() before retrieve(). Qdrant's local mode is a real vector
    index and can be selected with an injected QdrantClient(path=...) instance.
    Unknown publication dates are retained as unknown; known future dates are
    excluded by the server filter and verified again on each returned hit.
    """
    def __init__(
        self, *, corpus: Any, embedding: EmbeddingProvider, store: ArtifactStore,
        client: Any | None = None, index_revision: str | None = None,
        company_aliases: dict[str, str] | None = None,
        collection_name: str | None = None,
    ):
        self.corpus, self.embedding, self.store = corpus, embedding, store
        self.sources: dict[str, Source] = dict(corpus.sources)
        self.source_refs: dict[str, ArtifactRef] = dict(getattr(corpus, "source_refs", {}))
        self.evidence_refs: dict[str, ArtifactRef] = {}
        self.receipt_refs: dict[str, ArtifactRef] = {}
        self.company_aliases = dict(company_aliases or {})
        self._chunks = {chunk.chunk_id: chunk for chunk in corpus.chunks}
        if len(self._chunks) != len(corpus.chunks) or not self._chunks:
            raise ValueError("Index requires nonempty uniquely identified corpus chunks")
        for chunk in corpus.chunks:
            if chunk.source_id not in self.sources:
                raise ValueError("Chunk source is missing")
            if chunk.token_count > 400 or chunk.page_number < 1:
                raise ValueError("Chunk exceeds 400 tokens or has invalid PDF page")
        owners = {company_id for chunk in corpus.chunks for company_id in chunk.company_ids}
        for alias, owner in self.company_aliases.items():
            if owner not in owners or alias in owners and alias != owner:
                raise ValueError("Company alias conflicts with actual corpus ownership")
        self._owners = owners
        metadata = getattr(embedding, "metadata", None)
        if metadata is None:
            # Test/service providers must declare normalization explicitly too.
            if not hasattr(embedding, "normalize"):
                raise ValueError("Embedding provider must declare normalization policy")
            metadata = {"model_id": embedding.model_id, "revision": embedding.revision,
                        "dimension": embedding.dimension, "normalize": embedding.normalize,
                        "representation": "dense"}
        if any(metadata.get(key) != getattr(embedding, key) for key in ("model_id", "revision", "dimension")):
            raise ValueError("Embedding metadata differs from provider identity")
        self._normalize = metadata["normalize"]
        if type(self._normalize) is not bool:
            raise ValueError("Embedding normalization must be a recorded boolean")
        chunk_manifest = [
            _chunk_payload(c, corpus_hash=corpus.corpus_hash, index_hash="")
            for c in sorted(corpus.chunks, key=lambda c: c.chunk_id)
        ]
        self.manifest = {
            "schema_version": "1.0", "corpus_version": corpus.corpus_version,
            "corpus_hash": corpus.corpus_hash, "embedding": metadata,
            "chunk_manifest_hash": canonical_hash(chunk_manifest), "chunk_count": len(chunk_manifest),
            "distance": "Cosine", "chunk_tokens": 400,
            "source_hashes": {sid: canonical_hash(source.model_dump(mode="json", exclude={"retrieved_at"}))
                              for sid, source in sorted(self.sources.items())},
        }
        self.index_hash = canonical_hash(self.manifest)
        self.index_revision = _component(index_revision or self.index_hash[:20])
        self.collection_name = _component(collection_name) if collection_name else "neutral_v2_suhyeon_" + self.index_revision
        self._payloads = {c.chunk_id: _chunk_payload(c, corpus_hash=corpus.corpus_hash, index_hash=self.index_hash) for c in corpus.chunks}
        if client is None:
            from qdrant_client import QdrantClient
            client = QdrantClient(":memory:")
        self.client, self.manifest_ref = client, None
        self._initialized = False
        self._lock = asyncio.Lock()

    def _collection_metadata(self, *, ready: bool) -> dict[str, Any]:
        return {"neutral_index_manifest": self.manifest, "manifest_sha256": self.index_hash,
                "index_revision": self.index_revision, "ready": ready}

    def _verify_collection(self) -> None:
        try:
            info = self.client.get_collection(self.collection_name)
            vectors = info.config.params.vectors
            metadata = info.config.metadata
            if metadata != self._collection_metadata(ready=True):
                raise IndexMismatchError("Collection revision/model/corpus manifest differs or index is incomplete")
            if not hasattr(vectors, "size") or vectors.size != self.embedding.dimension or str(vectors.distance) not in {"Cosine", "Distance.COSINE"}:
                raise IndexMismatchError("Collection vector dimension or distance differs")
            if self.client.count(self.collection_name, exact=True).count != len(self._chunks):
                raise IndexMismatchError("Collection chunk count differs from immutable corpus")
        except IndexMismatchError:
            raise
        except Exception as exc:
            raise IndexMismatchError(f"Collection integrity check failed ({type(exc).__name__})") from exc

    async def initialize(self) -> ArtifactRef:
        """Build one immutable revision, or validate and reuse an identical one."""
        async with self._lock:
            exists = await asyncio.to_thread(self.client.collection_exists, self.collection_name)
            if exists:
                await asyncio.to_thread(self._verify_collection)
            else:
                from qdrant_client import models
                vectors = await self.embedding.embed_documents([c.text for c in self.corpus.chunks])
                vectors = validate_vectors(vectors, len(self.corpus.chunks), self.embedding.dimension, normalize=self._normalize)
                await asyncio.to_thread(
                    self.client.create_collection, self.collection_name,
                    vectors_config=models.VectorParams(size=self.embedding.dimension, distance=models.Distance.COSINE),
                    metadata=self._collection_metadata(ready=False),
                )
                # A cancelled/failed build remains marked incomplete. Never delete
                # or recreate that collection silently on a later attempt.
                points = [models.PointStruct(
                    id=str(uuid.uuid5(uuid.NAMESPACE_URL, self.index_hash + ":" + chunk.chunk_id)),
                    vector=vector, payload=self._payloads[chunk.chunk_id],
                ) for chunk, vector in zip(self.corpus.chunks, vectors, strict=True)]
                for start in range(0, len(points), 64):
                    await asyncio.to_thread(self.client.upsert, self.collection_name, points[start:start + 64], wait=True)
                await asyncio.to_thread(self.client.update_collection, self.collection_name, metadata=self._collection_metadata(ready=True))
                await asyncio.to_thread(self._verify_collection)
            for source_id, source in self.sources.items():
                if source_id in self.source_refs:
                    await self.store.verify(self.source_refs[source_id])
                else:
                    payload = source.model_dump(mode="json")
                    self.source_refs[source_id] = await self.store.save(
                        payload, key=f"sources/{canonical_hash(source_id)}/{canonical_hash(payload)}.json",
                    )
            self.manifest_ref = await self.store.save(
                self._collection_metadata(ready=True), key=f"indexes/{self.index_revision}.json",
            )
            self._initialized = True
            return self.manifest_ref

    async def _receipt(self, query: str, *, filters: dict[str, str], ranked_hits: list[RankedHit], outcome: str, query_id: str) -> RetrievalReceipt:
        receipt = RetrievalReceipt(
            query_id=query_id, query_text=query, queries_hash=canonical_hash([query]),
            provider=self.embedding.model_id, corpus_version=self.corpus.corpus_version,
            index_version=self.index_revision, filters=filters, ranked_hits=ranked_hits,
            outcome=outcome, ts=datetime.now(timezone.utc),
        )
        self.receipt_refs[query_id] = await self.store.save(receipt.model_dump(mode="json"), key=f"retrieval/{query_id}.json")
        return receipt

    async def _verify_artifact(self, ref: ArtifactRef, *, load: bool = False) -> Any:
        try:
            await self.store.verify(ref)
            return await self.store.load(ref) if load else None
        except SourceInaccessibleError:
            raise
        except Exception as exc:
            raise ArtifactIntegrityError(f"Persisted artifact integrity failed ({type(exc).__name__})") from exc

    def _filter(self, *, company_id: str, domain: Domain, as_of_date: date, excluded: Iterable[str]) -> Any:
        from qdrant_client import models
        def not_future(key: str) -> Any:
            return models.Filter(should=[
                models.FieldCondition(key=key, range=models.DatetimeRange(lte=as_of_date)),
                models.IsNullCondition(is_null=models.PayloadField(key=key)),
            ])
        return models.Filter(must=[
            models.FieldCondition(key="company_ids", match=models.MatchValue(value=company_id)),
            models.FieldCondition(key="domain", match=models.MatchValue(value=domain.value)),
            models.FieldCondition(key="index_hash", match=models.MatchValue(value=self.index_hash)),
            not_future("published_at"), not_future("event_date"),
        ], must_not=[models.FieldCondition(key="source_id", match=models.MatchAny(any=list(excluded)))] if excluded else None)

    async def retrieve(
        self, query: str, *, company_id: str, domain: Domain, as_of_date: date,
        top_k: int = 5, excluded_source_ids: Iterable[str] = (),
    ) -> RetrievalResult:
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise ValueError("Query must be nonempty and at most 2000 characters")
        if type(top_k) is not int or not 1 <= top_k <= 5 or type(as_of_date) is not date:
            raise ValueError("Retrieval requires top_k 1..5 and an actual as-of date")
        domain = Domain(domain)
        owner = self.company_aliases.get(company_id, company_id)
        excluded = tuple(sorted(set(excluded_source_ids)))
        query_id = "query_" + uuid.uuid4().hex
        filters = {"company_id": owner, "domain": domain.value, "as_of_date": as_of_date.isoformat(),
                   "top_k": str(top_k), "publication_date_policy": "exclude_future_preserve_unknown",
                   "excluded_source_ids_hash": canonical_hash(excluded)}
        if owner != company_id:
            filters["company_alias"] = company_id
        ranked: list[RankedHit] = []
        async with self._lock:
            try:
                if not self._initialized:
                    raise IndexMismatchError("Retriever has not initialized a validated index")
                if not any(c.domain == domain for c in self.corpus.chunks):
                    raise CorpusDomainUnavailableError("No prepared PDF corpus for requested domain")
                await asyncio.to_thread(self._verify_collection)
                await self._verify_artifact(self.manifest_ref)
                if owner not in self._owners:
                    receipt = await self._receipt(query, filters=filters, ranked_hits=[], outcome="empty", query_id=query_id)
                    return RetrievalResult(sources=[], evidence=[], receipt=receipt)
                vector = await self.embedding.embed_query(query)
                vector = validate_vectors([vector], 1, self.embedding.dimension, normalize=self._normalize)[0]
                response = await asyncio.to_thread(
                    self.client.query_points, self.collection_name, query=vector,
                    query_filter=self._filter(company_id=owner, domain=domain, as_of_date=as_of_date, excluded=excluded),
                    limit=top_k, with_payload=True,
                )
                evidence, sources, seen, content_cache = [], {}, set(), {}
                for index, point in enumerate(response.points, start=1):
                    payload = point.payload or {}
                    chunk = self._chunks.get(payload.get("chunk_id"))
                    if chunk is None or payload != self._payloads[chunk.chunk_id]:
                        raise IndexMismatchError("Retrieved chunk content or ownership differs from corpus")
                    if owner not in chunk.company_ids or chunk.domain != domain or any(
                        d is not None and d > as_of_date for d in (chunk.published_at, chunk.event_date)
                    ):
                        raise IndexMismatchError("Returned chunk violates requested company/domain/date")
                    identity = (chunk.source_id, chunk.chunk_id)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    ranked.append(RankedHit(source_id=chunk.source_id, chunk_id=chunk.chunk_id, rank=index, score=float(point.score)))
                    source = self.sources[chunk.source_id]
                    if source.source_id not in content_cache:
                        content_cache[source.source_id] = await self._verify_artifact(source.content_ref, load=True)
                    content = content_cache[source.source_id]
                    pages = [page for page in content.get("pages", []) if page.get("page_number") == chunk.page_number]
                    if (
                        not content.get("body_verified") or len(pages) != 1
                        or owner not in content.get("company_ids", []) or content.get("domain") != domain.value
                        or content.get("original_pdf_sha256") != chunk.original_sha256
                        or chunk.char_start < 0 or chunk.char_end <= chunk.char_start
                        or pages[0]["text"][chunk.char_start:chunk.char_end] != chunk.text
                    ):
                        raise IndexMismatchError("Chunk quote or attribution differs from the acquired original PDF page")
                    item = Evidence(
                        evidence_id="evidence_" + canonical_hash([owner, chunk.source_id, chunk.chunk_id, chunk.text])[:32],
                        source_id=chunk.source_id, company_id=owner, chunk_id=chunk.chunk_id,
                        page_number=chunk.page_number, quote=chunk.text, event_date=chunk.event_date,
                    )
                    self.evidence_refs[item.evidence_id] = await self.store.save(item.model_dump(mode="json"), key=f"evidence/{item.evidence_id}.json")
                    evidence.append(item)
                    sources[source.source_id] = source
                receipt = await self._receipt(query, filters=filters, ranked_hits=ranked, outcome="success" if evidence else "empty", query_id=query_id)
                return RetrievalResult(sources=list(sources.values()), evidence=evidence, receipt=receipt)
            except (IndexMismatchError, CorpusDomainUnavailableError, ArtifactIntegrityError) as exc:
                exc.receipt = await self._receipt(query, filters=filters, ranked_hits=ranked, outcome="error", query_id=query_id)
                raise
            except SourceInaccessibleError:
                receipt = await self._receipt(query, filters=filters, ranked_hits=ranked, outcome="source_inaccessible", query_id=query_id)
                return RetrievalResult(sources=[], evidence=[], receipt=receipt)
            except Exception:
                receipt = await self._receipt(query, filters=filters, ranked_hits=ranked, outcome="error", query_id=query_id)
                return RetrievalResult(sources=[], evidence=[], receipt=receipt)


def ranking_metrics(query_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """HitRate@5 and MRR@5 preserve actual chunk ranks (no source re-ranking)."""
    groups: dict[str, list[tuple[float, float]]] = {"overall": []}
    for row in query_rows:
        relevant = set(row["relevant_source_ids"])
        ranks = [hit["rank"] for hit in row["ranked_hits"] if hit["source_id"] in relevant and hit["rank"] <= 5]
        score = (float(bool(ranks)), 1.0 / min(ranks) if ranks else 0.0)
        groups["overall"].append(score)
        groups.setdefault(row["perspective"], []).append(score)
    return {key: {"query_count": len(values), "hit_rate_at_5": sum(v[0] for v in values) / len(values) if values else 0.0,
                  "mrr_at_5": sum(v[1] for v in values) / len(values) if values else 0.0} for key, values in groups.items()}
