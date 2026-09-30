"""시장 문서의 Qdrant 검색·색인입니다. 평가·웹 검색은 에이전트가 담당합니다."""

from __future__ import annotations

import math
from pathlib import Path
from threading import RLock
from main.paths import DEFAULT_PDF_DIR, DEFAULT_MANIFEST_PATH
from main.rag.embeddings import (
    EmbeddingBackend,
    EmbeddingProviderError,
    OpenAIEmbeddings,
    validate_vectors,
)
from .models import (
    MarketChunk,
    MarketSearchHit,
    MarketRAGProviderError,
    MarketRAGStoreError,
    MarketRAGIndexNotReady,
    MarketRAGClosedError,
)
from .pdf_data import _load_manifest, prepare_chunks

MARKET_COLLECTION_NAME = "market_reference_pdf_512"


class MarketRAG:
    """시장성 평가의 공통 PDF 근거를 준비하고 검색합니다.

    ``build_index()``는 최초 실행 전에 한 번 호출해야 합니다. 이미 컬렉션이
    있으면 재사용하며, PDF나 sources.json을 변경한 경우에만
    ``build_index(rebuild=True)``로 다시 만듭니다.
    """

    def __init__(
        self,
        pdf_dir: str | Path = DEFAULT_PDF_DIR,
        manifest_path: str | Path = DEFAULT_MANIFEST_PATH,
        *,
        model_name: str = "text-embedding-3-small",
        dimensions: int = 512,
        qdrant_url: str = "http://localhost:6333",
        embeddings: EmbeddingBackend | None = None,
    ):
        if model_name != "text-embedding-3-small":
            raise ValueError("Only text-embedding-3-small is supported in this project")
        if type(dimensions) is not int or not 1 <= dimensions <= 1_536:
            raise ValueError("dimensions must be an integer from 1 to 1536")
        if embeddings is not None and (
            embeddings.model_name != model_name or embeddings.dimensions != dimensions
        ):
            raise ValueError(
                "Injected embedding settings must match MarketRAG settings"
            )

        self.pdf_dir = Path(pdf_dir).expanduser().resolve()
        self.manifest_path = Path(manifest_path).expanduser().resolve()
        self.model_name = model_name
        self.dimensions = dimensions
        self.qdrant_url = qdrant_url

        self._lock = RLock()
        self._client = None
        self._embeddings = embeddings
        self._owns_embeddings = embeddings is None
        self._ready = False
        self._closed = False

        # 생성 시점에 파일 구성을 먼저 검증하면 잘못된 경로를 색인 시점보다
        # 빠르게 발견할 수 있습니다. PDF 본문 추출은 prepare_chunks()에서 합니다.
        self._sources = _load_manifest(self.manifest_path, self.pdf_dir)

    def _ensure_open(self) -> None:
        if self._closed:
            raise MarketRAGClosedError("Create a new MarketRAG instance after close()")

    def _store_call(self, method: str, **kwargs):
        """Qdrant 오류를 빈 검색 결과와 구분해서 전달합니다."""

        try:
            if self._client is None:
                from qdrant_client import QdrantClient

                self._client = QdrantClient(url=self.qdrant_url, timeout=15)
            return getattr(self._client, method)(**kwargs)
        except Exception as exc:
            raise MarketRAGStoreError(
                f"Qdrant {method} failed ({type(exc).__name__}); check the server"
            ) from exc

    def _embed_documents(self, texts: list[str]) -> list[list[float]]:
        if self._embeddings is None:
            self._embeddings = OpenAIEmbeddings(self.model_name, self.dimensions)
        try:
            vectors = self._embeddings.embed_documents(texts)
        except EmbeddingProviderError as exc:
            raise MarketRAGProviderError(str(exc)) from exc
        except (MarketRAGProviderError, ValueError):
            raise
        except Exception as exc:
            raise MarketRAGProviderError(
                f"Embedding backend failed ({type(exc).__name__})"
            ) from exc
        try:
            return validate_vectors(vectors, len(texts), self.dimensions)
        except EmbeddingProviderError as exc:
            raise MarketRAGProviderError(str(exc)) from exc

    def _embed_query(self, query: str) -> list[float]:
        if self._embeddings is None:
            self._embeddings = OpenAIEmbeddings(self.model_name, self.dimensions)
        try:
            vector = self._embeddings.embed_query(query)
        except EmbeddingProviderError as exc:
            raise MarketRAGProviderError(str(exc)) from exc
        except (MarketRAGProviderError, ValueError):
            raise
        except Exception as exc:
            raise MarketRAGProviderError(
                f"Embedding backend failed ({type(exc).__name__})"
            ) from exc
        try:
            return validate_vectors([vector], 1, self.dimensions)[0]
        except EmbeddingProviderError as exc:
            raise MarketRAGProviderError(str(exc)) from exc

    def prepare_chunks(self) -> list[MarketChunk]:
        """PDF 준비를 데이터 모듈에 맡깁니다. OpenAI·Qdrant 호출은 없습니다."""
        with self._lock:
            self._ensure_open()
            return prepare_chunks(self._sources, self.pdf_dir)

    def build_index(self, *, rebuild: bool = False) -> int:
        """시장 PDF 컬렉션을 만들거나 기존 컬렉션을 재사용합니다."""

        with self._lock:
            self._ensure_open()
            if type(rebuild) is not bool:
                raise ValueError("rebuild must be bool")

            exists = self._store_call(
                "collection_exists", collection_name=MARKET_COLLECTION_NAME
            )
            if exists and not rebuild:
                self._ready = False
                info = self._store_call(
                    "get_collection", collection_name=MARKET_COLLECTION_NAME
                )
                vectors = info.config.params.vectors
                if getattr(vectors, "size", None) != self.dimensions:
                    raise ValueError(
                        "Market collection dimension differs; run build_index(rebuild=True)"
                    )
                count = self._store_call(
                    "count", collection_name=MARKET_COLLECTION_NAME, exact=True
                ).count
                self._ready = True
                return count

            # 기존 컬렉션을 지우기 전에 PDF 추출과 임베딩을 모두 끝냅니다.
            # 준비 과정이 실패하면 기존에 정상 동작하던 컬렉션은 그대로 남습니다.
            chunks = self.prepare_chunks()
            vectors = self._embed_documents([chunk.text for chunk in chunks])

            from qdrant_client import models

            points = [
                models.PointStruct(
                    id=chunk.chunk_id,
                    vector=vector,
                    payload=chunk.model_dump(),
                )
                for chunk, vector in zip(chunks, vectors)
            ]

            self._ready = False
            try:
                if exists:
                    self._store_call(
                        "delete_collection", collection_name=MARKET_COLLECTION_NAME
                    )
                self._store_call(
                    "create_collection",
                    collection_name=MARKET_COLLECTION_NAME,
                    vectors_config=models.VectorParams(
                        size=self.dimensions,
                        distance=models.Distance.COSINE,
                    ),
                )
                # 한 번에 너무 많은 포인트를 보내지 않도록 작은 배치로 저장합니다.
                for start in range(0, len(points), 64):
                    self._store_call(
                        "upsert",
                        collection_name=MARKET_COLLECTION_NAME,
                        points=points[start : start + 64],
                        wait=True,
                    )
                count = self._store_call(
                    "count", collection_name=MARKET_COLLECTION_NAME, exact=True
                ).count
            except MarketRAGStoreError as exc:
                raise MarketRAGStoreError(
                    "Market index replacement failed; run build_index(rebuild=True) again"
                ) from exc

            self._ready = True
            return count

    @staticmethod
    def _validate_optional_filter(
        name: str, values: list[str] | None
    ) -> list[str] | None:
        if values is None:
            return None
        if (
            not isinstance(values, list)
            or not values
            or any(not isinstance(value, str) or not value.strip() for value in values)
        ):
            raise ValueError(f"{name} must be a nonempty list of strings")
        return [value.strip() for value in values]

    def retrieve(
        self,
        query: str,
        n_results: int = 5,
        *,
        document_ids: list[str] | None = None,
        market_segments: list[str] | None = None,
        regions: list[str] | None = None,
    ) -> list[MarketSearchHit]:
        """질문과 관련된 PDF 청크를 문서·페이지 정보와 함께 반환합니다."""

        with self._lock:
            self._ensure_open()
            if not self._ready:
                raise MarketRAGIndexNotReady("Call build_index() before retrieve()")
            if not isinstance(query, str) or not query.strip():
                raise ValueError("query must be a nonempty string")
            if type(n_results) is not int or n_results < 1:
                raise ValueError("n_results must be a positive integer")

            document_ids = self._validate_optional_filter("document_ids", document_ids)
            market_segments = self._validate_optional_filter(
                "market_segments", market_segments
            )
            regions = self._validate_optional_filter("regions", regions)

            from qdrant_client import models

            conditions = []
            if document_ids:
                conditions.append(
                    models.FieldCondition(
                        key="document_id",
                        match=models.MatchAny(any=document_ids),
                    )
                )
            if market_segments:
                conditions.append(
                    models.FieldCondition(
                        key="market_segments",
                        match=models.MatchAny(any=market_segments),
                    )
                )
            if regions:
                conditions.append(
                    models.FieldCondition(
                        key="region",
                        match=models.MatchAny(any=regions),
                    )
                )

            response = self._store_call(
                "query_points",
                collection_name=MARKET_COLLECTION_NAME,
                query=self._embed_query(query.strip()),
                query_filter=models.Filter(must=conditions) if conditions else None,
                limit=n_results,
                with_payload=True,
                with_vectors=False,
            )

            hits: list[MarketSearchHit] = []
            for point in response.points:
                if not math.isfinite(point.score):
                    raise MarketRAGStoreError(
                        "Qdrant returned a nonfinite similarity score"
                    )
                try:
                    chunk = MarketChunk.model_validate(point.payload or {})
                except Exception as exc:
                    raise MarketRAGStoreError(
                        "Qdrant payload does not match MarketChunk; rebuild the index"
                    ) from exc
                hits.append(MarketSearchHit(chunk=chunk, score=point.score))
            return hits

    @property
    def doc_count(self) -> int:
        """준비된 Qdrant 시장 청크 수를 반환합니다."""

        with self._lock:
            if self._closed or not self._ready:
                return 0
            return self._store_call(
                "count", collection_name=MARKET_COLLECTION_NAME, exact=True
            ).count

    def close(self) -> None:
        """네트워크 클라이언트만 닫고 Qdrant 컬렉션은 보존합니다."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._ready = False
            try:
                if self._client is not None:
                    self._client.close()
            finally:
                if self._owns_embeddings and self._embeddings is not None:
                    close = getattr(self._embeddings, "close", None)
                    if callable(close):
                        close()
