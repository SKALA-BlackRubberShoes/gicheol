"""기업 한 행의 정확 조회와 Qdrant 검색·색인 서비스입니다.

회사 선택과 그래프 State 관리는 호출하는 에이전트가 담당합니다.
"""

from __future__ import annotations

import math
from pathlib import Path
from threading import RLock
from uuid import NAMESPACE_URL, uuid5

from main.paths import DEFAULT_CSV_PATH
from main.rag.embeddings import (
    EmbeddingBackend,
    EmbeddingProviderError,
    OpenAIEmbeddings,
    validate_vectors,
)
from .data import _load_csv, _matches, _validate_filters, _validate_id
from .models import (
    CompanyFilter,
    CompanyRecord,
    RAGClosedError,
    RAGCompanyNotFoundError,
    RAGIndexNotReady,
    RAGProviderError,
    RAGStoreError,
    RAGUnknownFieldError,
    Scalar,
    SearchHit,
)

COLLECTION_NAME = "companies_small_512"


class BaseRAG:
    """
    여러 에이전트가 공유하는 기업 정보 조회·검색 모듈입니다.

    생성 시 CSV만 읽고, build_index()에서 Qdrant 색인을 준비합니다.
    기존 컬렉션은 차원만 확인해 재사용하며 CSV 변경 시 rebuild=True로 갱신합니다.
    기본 CSV는 프로젝트의 docs/data/base/raw/에 있습니다.
    """

    # ──────────────────────────────────────────
    # 초기화
    # ──────────────────────────────────────────

    def __init__(
        self,
        csv_path: str | Path = DEFAULT_CSV_PATH,
        *,
        model_name: str = "text-embedding-3-small",
        dimensions: int = 512,
        qdrant_url: str = "http://localhost:6333",
        embeddings: EmbeddingBackend | None = None,
    ):
        if model_name != "text-embedding-3-small":
            raise ValueError("Only text-embedding-3-small is supported in this version")
        if type(dimensions) is not int or not 1 <= dimensions <= 1536:
            raise ValueError("dimensions must be an integer from 1 to 1536")
        if embeddings is not None and (
            embeddings.model_name != model_name or embeddings.dimensions != dimensions
        ):
            raise ValueError(
                "Injected embedding model/dimensions must match the configuration"
            )
        self.csv_path = Path(csv_path).expanduser().resolve()
        self.model_name, self.dimensions, self.qdrant_url = (
            model_name,
            dimensions,
            qdrant_url,
        )
        # 정확 조회는 이 스냅샷을 사용하므로 OpenAI·Qdrant 연결이 필요 없습니다.
        self._records = _load_csv(self.csv_path)
        self._lock = RLock()
        self._closed = self._ready = False
        self._client = None
        self._embeddings = embeddings
        self._owns_embeddings = embeddings is None

    def _ensure_open(self):
        """종료한 객체를 다시 사용하지 않도록 확인합니다."""
        if self._closed:
            raise RAGClosedError("Create a new BaseRAG instance after close()")

    # ──────────────────────────────────────────
    # Public API — CSV 정확 조회
    # ──────────────────────────────────────────

    def get_company(self, company_id: str) -> CompanyRecord | None:
        """회사 한 행을 반환합니다. 없는 ID는 None이며 반환값은 원본의 복사본입니다."""
        with self._lock:
            self._ensure_open()
            record = self._records.get(_validate_id(company_id))
            return record.model_copy(deep=True) if record is not None else None

    def get_field(self, company_id: str, field: str, *, raw: bool = False) -> Scalar:
        """요청한 항목 하나만 반환합니다. raw=True이면 CSV 컬럼명으로 원문을 조회합니다."""
        return self.get_fields(company_id, [field], raw=raw)[field]

    def get_fields(
        self, company_id: str, fields: list[str], *, raw: bool = False
    ) -> dict[str, Scalar]:
        """요청한 항목만 순서대로 반환합니다. 없는 회사·항목은 실제 결측과 구분합니다."""
        with self._lock:
            self._ensure_open()
            if (
                not isinstance(fields, list)
                or not fields
                or any(not isinstance(f, str) or not f.strip() for f in fields)
                or len(fields) != len(set(fields))
                or type(raw) is not bool
            ):
                raise ValueError(
                    "fields must be a nonempty list of unique strings; raw must be bool"
                )
            record = self._records.get(_validate_id(company_id))
            if record is None:
                raise RAGCompanyNotFoundError(f"Unknown company_id: {company_id}")
            values = record.raw if raw else record.values
            unknown = [f for f in fields if f not in values]
            if unknown:
                raise RAGUnknownFieldError(
                    f"Unknown {'raw' if raw else 'normalized'} fields: {unknown}"
                )
            return {f: values[f] for f in fields}

    def list_companies(
        self, *, filters: list[CompanyFilter] | None = None
    ) -> list[CompanyRecord]:
        """조건을 만족하는 전체 회사를 ID 사전순으로 반환합니다. API 호출은 없습니다."""
        with self._lock:
            self._ensure_open()
            filters = _validate_filters(filters)
            return [
                r.model_copy(deep=True)
                for _, r in sorted(self._records.items())
                if _matches(r, filters)
            ]

    # ──────────────────────────────────────────
    # 내부 클라이언트 호출
    # ──────────────────────────────────────────

    def _store_call(self, method: str, **kwargs):
        """Qdrant 작업을 실행하고 서버 오류를 검색 결과 없음과 구분합니다."""
        try:
            if self._client is None:
                from qdrant_client import QdrantClient

                self._client = QdrantClient(url=self.qdrant_url, timeout=10)
            return getattr(self._client, method)(**kwargs)
        except Exception as exc:
            raise RAGStoreError(
                f"Qdrant {method} failed ({type(exc).__name__}); check the server connection"
            ) from exc

    def _embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        """설정된 임베더로 문서 또는 질문 벡터를 생성합니다."""
        if not texts:
            return []
        if self._embeddings is None:
            self._embeddings = OpenAIEmbeddings(self.model_name, self.dimensions)
        try:
            vectors = (
                [self._embeddings.embed_query(texts[0])]
                if query
                else self._embeddings.embed_documents(texts)
            )
            return validate_vectors(vectors, len(texts), self.dimensions)
        except EmbeddingProviderError as exc:
            raise RAGProviderError(str(exc)) from exc
        except (RAGProviderError, ValueError):
            raise
        except Exception as exc:
            raise RAGProviderError(
                f"Embedding backend failed ({type(exc).__name__})"
            ) from exc

    # ──────────────────────────────────────────
    # Public API — 색인 생성과 의미 검색
    # ──────────────────────────────────────────

    def build_index(self, *, rebuild: bool = False) -> int:
        """
        컬렉션이 없으면 생성하고, 있으면 차원 확인 후 재사용합니다.
        rebuild=True이면 CSV를 다시 읽어 전체 색인을 교체합니다. 반환값은 서버 문서 수입니다.
        """
        with self._lock:
            self._ensure_open()
            if type(rebuild) is not bool:
                raise ValueError("rebuild must be bool")
            exists = self._store_call(
                "collection_exists", collection_name=COLLECTION_NAME
            )
            if exists and not rebuild:
                self._ready = False
                vectors = self._store_call(
                    "get_collection", collection_name=COLLECTION_NAME
                ).config.params.vectors
                if getattr(vectors, "size", None) != self.dimensions:
                    raise ValueError(
                        "Collection dimension differs; run build_index(rebuild=True)"
                    )
                count = self._store_call(
                    "count", collection_name=COLLECTION_NAME, exact=True
                ).count
                self._ready = True
                return count

            # CSV 검증과 임베딩을 먼저 마칩니다. 여기서 실패하면 기존 색인을 유지합니다.
            records = _load_csv(self.csv_path) if rebuild else self._records
            ordered = [r for _, r in sorted(records.items())]
            vectors = self._embed([r.content for r in ordered])
            from qdrant_client import models

            # 회사 ID에서 UUID를 만들므로 CSV 행 순서가 바뀌어도 point ID가 유지됩니다.
            points = [
                models.PointStruct(
                    id=str(uuid5(NAMESPACE_URL, "basic-rag:" + r.company_id)),
                    vector=vector,
                    payload={
                        "company_id": r.company_id,
                        "content": r.content,
                        "raw": r.raw,
                        "values": r.values,
                    },
                )
                for r, vector in zip(ordered, vectors)
            ]
            self._ready = False
            try:
                if exists:
                    self._store_call(
                        "delete_collection", collection_name=COLLECTION_NAME
                    )
                self._store_call(
                    "create_collection",
                    collection_name=COLLECTION_NAME,
                    vectors_config=models.VectorParams(
                        size=self.dimensions, distance=models.Distance.COSINE
                    ),
                )
                if points:
                    self._store_call(
                        "upsert",
                        collection_name=COLLECTION_NAME,
                        points=points,
                        wait=True,
                    )
                count = self._store_call(
                    "count", collection_name=COLLECTION_NAME, exact=True
                ).count
            except RAGStoreError as exc:
                raise RAGStoreError(
                    "Index replacement failed; run build_index(rebuild=True) again"
                ) from exc
            self._records, self._ready = records, True
            return count

    def retrieve(
        self,
        query: str,
        n_results: int = 5,
        *,
        company_id: str | None = None,
        filters: list[CompanyFilter] | None = None,
        exclude_company_id: str | None = None,
    ) -> list[SearchHit]:
        """
        질문과 유사한 기업 후보를 반환합니다. build_index() 후 호출합니다.
        전체 CSV에 조건을 먼저 적용하고, 해당 회사들만 Qdrant에서 검색합니다.
        """
        with self._lock:
            self._ensure_open()
            if not self._ready:
                raise RAGIndexNotReady("Call build_index() before retrieve()")
            if (
                not isinstance(query, str)
                or not query.strip()
                or type(n_results) is not int
                or n_results < 1
            ):
                raise ValueError(
                    "query must be nonempty and n_results a positive integer"
                )
            filters = _validate_filters(filters)
            company_id = _validate_id(company_id) if company_id is not None else None
            exclude_company_id = (
                _validate_id(exclude_company_id)
                if exclude_company_id is not None
                else None
            )
            eligible = [
                r.company_id
                for r in self._records.values()
                if (company_id is None or r.company_id == company_id)
                and r.company_id != exclude_company_id
                and _matches(r, filters)
            ]
            if not eligible:
                return []
            vector = self._embed([query], query=True)[0]
            from qdrant_client import models

            response = self._store_call(
                "query_points",
                collection_name=COLLECTION_NAME,
                query=vector,
                query_filter=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="company_id", match=models.MatchAny(any=eligible)
                        )
                    ]
                ),
                limit=len(eligible),
                with_payload=True,
                with_vectors=False,
            )
            hits = []
            allowed = set(eligible)
            for point in response.points:
                result_id = (point.payload or {}).get("company_id")
                if not isinstance(result_id, str) or result_id not in allowed:
                    raise RAGStoreError(
                        "Unknown/out-of-scope company_id in Qdrant; run build_index(rebuild=True)"
                    )
                if not math.isfinite(point.score):
                    raise RAGStoreError("Qdrant returned a nonfinite similarity score")
                # 서버에는 벡터를 묻고, 반환할 회사 정보는 현재 CSV 스냅샷에서 가져옵니다.
                hits.append(
                    SearchHit(
                        company=self._records[result_id].model_copy(deep=True),
                        score=point.score,
                    )
                )
            return sorted(hits, key=lambda hit: (-hit.score, hit.company.company_id))[
                :n_results
            ]

    # ──────────────────────────────────────────
    # Public API — 상태 조회와 종료
    # ──────────────────────────────────────────

    @property
    def doc_count(self) -> int:
        """서버의 문서 수를 반환합니다. 준비 전·종료 후는 0이며 서버 장애는 오류로 알립니다."""
        with self._lock:
            if self._closed or not self._ready:
                return 0
            return self._store_call(
                "count", collection_name=COLLECTION_NAME, exact=True
            ).count

    def close(self) -> None:
        """클라이언트만 닫습니다. Qdrant 서버·컬렉션·볼륨은 유지합니다."""
        with self._lock:
            if self._closed:
                return
            self._closed, self._ready = True, False
            try:
                if self._client is not None:
                    self._client.close()
            finally:
                if self._owns_embeddings and self._embeddings is not None:
                    self._embeddings.close()
