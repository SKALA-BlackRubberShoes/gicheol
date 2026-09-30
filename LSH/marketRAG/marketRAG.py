"""시장성 평가용 PDF RAG.

이 모듈의 책임은 의도적으로 작게 유지합니다.

1. ``LSH/market/raw``의 PDF에서 페이지별 텍스트를 읽습니다.
2. 긴 페이지를 검색하기 좋은 크기로 나눕니다.
3. 청크를 OpenAI 임베딩으로 변환해 Qdrant에 저장합니다.
4. 질문과 의미가 가까운 청크를 문서명·페이지와 함께 반환합니다.

기업 선택, 웹 검색, 1~5점 평가와 가중점수 계산은 이 모듈이 담당하지
않습니다. 그 작업은 ``LSH.marketEvaluation`` 패키지에서 수행합니다.
"""

from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from threading import RLock
from typing import Protocol
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field
from pypdf import PdfReader


# LSH 폴더를 기준으로 기본 데이터 경로를 계산합니다. 이렇게 하면 팀원의
# 컴퓨터에서 저장소 위치가 달라져도 개인 절대경로를 수정할 필요가 없습니다.
LSH_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PDF_DIR = LSH_ROOT / "market/raw"
DEFAULT_MANIFEST_PATH = LSH_ROOT / "market/sources.json"

# 기존 BaseRAG의 companies_small_512와 절대 섞지 않습니다.
MARKET_COLLECTION_NAME = "market_reference_pdf_512"


# ---------------------------------------------------------------------------
# 오류 타입
# ---------------------------------------------------------------------------


class MarketRAGDataError(ValueError):
    """PDF 또는 sources.json의 형식과 내용이 잘못됐을 때 발생합니다."""


class MarketRAGProviderError(RuntimeError):
    """OpenAI 임베딩 호출 또는 응답 검증이 실패했을 때 발생합니다."""


class MarketRAGStoreError(RuntimeError):
    """Qdrant 연결, 저장 또는 검색이 실패했을 때 발생합니다."""


class MarketRAGIndexNotReady(RuntimeError):
    """색인을 준비하지 않은 상태에서 검색했을 때 발생합니다."""


class MarketRAGClosedError(RuntimeError):
    """close() 이후 같은 객체를 다시 사용했을 때 발생합니다."""


# ---------------------------------------------------------------------------
# 입력 문서와 검색 결과 모델
# ---------------------------------------------------------------------------


class MarketDocumentSource(BaseModel):
    """sources.json에 기록된 공식 문서 메타데이터입니다."""

    model_config = ConfigDict(extra="forbid")

    document_id: str = Field(min_length=1)
    file_name: str = Field(min_length=1)
    title: str = Field(min_length=1)
    publisher: str = Field(min_length=1)
    published_year: int = Field(ge=1900, le=2100)
    source_url: str | None = None
    region: str = Field(min_length=1)
    language: str = Field(min_length=2)
    market_segments: list[str] = Field(min_length=1)


class MarketChunk(BaseModel):
    """Qdrant에 저장되는 최소 검색 단위입니다."""

    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    document_id: str
    title: str
    publisher: str
    published_year: int
    source_url: str | None
    source_file: str
    page: int = Field(ge=1)
    chunk_index: int = Field(ge=0)
    region: str
    language: str
    market_segments: list[str]
    text: str = Field(min_length=1)


class MarketSearchHit(BaseModel):
    """검색된 청크와 코사인 유사도입니다.

    ``score``는 검색 관련도일 뿐 시장성 평가점수나 사실의 신뢰도가 아닙니다.
    """

    chunk: MarketChunk
    score: float


class EmbeddingBackend(Protocol):
    """운영용 OpenAI 임베더와 테스트용 가짜 임베더의 공통 규격입니다."""

    model_name: str
    dimensions: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


# ---------------------------------------------------------------------------
# 텍스트와 메타데이터 준비
# ---------------------------------------------------------------------------


def _clean_page_text(text: str) -> str:
    """PDF 추출 과정에서 생기는 과도한 공백을 읽기 쉬운 형태로 정리합니다."""

    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _chunk_page_text(
    text: str,
    *,
    max_chars: int = 1_800,
    overlap_chars: int = 200,
) -> list[str]:
    """한 페이지를 문자 수 기준의 겹치는 청크로 나눕니다.

    PDF마다 한국어·영어의 토큰 비율이 다르므로 청킹 자체는 단순하고 재현 가능한
    문자 수 기준으로 수행합니다. 임베딩 API의 실제 토큰 한도는 임베더가 별도로
    확인합니다. 청크는 한 페이지를 넘지 않으므로 검색 결과의 페이지 인용이
    항상 명확합니다.
    """

    if max_chars < 200:
        raise ValueError("max_chars must be at least 200")
    if overlap_chars < 0 or overlap_chars >= max_chars:
        raise ValueError("overlap_chars must be >= 0 and smaller than max_chars")

    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        hard_end = min(start + max_chars, len(text))
        end = hard_end

        # 가능한 경우 문장 또는 줄의 끝에서 자릅니다. 너무 앞에서 잘려 청크가
        # 지나치게 작아지는 경우에는 원래의 hard_end를 그대로 사용합니다.
        if hard_end < len(text):
            candidates = [
                text.rfind(separator, start + max_chars // 2, hard_end)
                for separator in ("\n", ". ", "다. ", "요. ")
            ]
            boundary = max(candidates)
            if boundary > start:
                end = boundary + 1

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(end - overlap_chars, start + 1)

    return chunks


def _load_manifest(path: Path, pdf_dir: Path) -> list[MarketDocumentSource]:
    """sources.json을 검증하고 실제 PDF 파일과 1:1로 대응시킵니다."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MarketRAGDataError(f"Cannot read market source manifest {path}: {exc}") from exc

    if not isinstance(raw, list) or not raw:
        raise MarketRAGDataError("Market source manifest must be a nonempty JSON array")

    try:
        sources = [MarketDocumentSource.model_validate(item) for item in raw]
    except Exception as exc:
        raise MarketRAGDataError(f"Invalid market source metadata: {exc}") from exc

    ids = [source.document_id for source in sources]
    names = [source.file_name for source in sources]
    if len(ids) != len(set(ids)):
        raise MarketRAGDataError("document_id values in sources.json must be unique")
    if len(names) != len(set(names)):
        raise MarketRAGDataError("file_name values in sources.json must be unique")

    missing = [name for name in names if not (pdf_dir / name).is_file()]
    if missing:
        raise MarketRAGDataError(f"PDF files listed in sources.json are missing: {missing}")

    # manifest에 등록되지 않은 PDF를 조용히 무시하지 않습니다. 새 문서를 추가한
    # 팀원이 출처 URL과 발행기관도 함께 기록하도록 하기 위한 검사입니다.
    actual = {path.name for path in pdf_dir.glob("*.pdf")}
    unregistered = sorted(actual - set(names))
    if unregistered:
        raise MarketRAGDataError(f"PDF files are not registered in sources.json: {unregistered}")

    return sources


# ---------------------------------------------------------------------------
# OpenAI 임베딩 어댑터
# ---------------------------------------------------------------------------


def _validate_vectors(
    vectors: list[list[float]],
    *,
    expected_count: int,
    dimensions: int,
) -> list[list[float]]:
    """임베딩 개수·차원·숫자 유효성을 Qdrant 저장 전에 확인합니다."""

    if not isinstance(vectors, list) or len(vectors) != expected_count:
        raise MarketRAGProviderError("Embedding response count does not match input")
    for vector in vectors:
        if (
            not isinstance(vector, list)
            or len(vector) != dimensions
            or any(type(value) not in (int, float) or not math.isfinite(value) for value in vector)
        ):
            raise MarketRAGProviderError("Invalid embedding dimension or numeric value")
        if math.sqrt(sum(value * value for value in vector)) == 0:
            raise MarketRAGProviderError("Embedding vector must not be a zero vector")
    return vectors


class _OpenAIEmbeddings:
    """text-embedding-3-small을 지연 초기화해 사용하는 기본 임베더입니다."""

    def __init__(self, model_name: str, dimensions: int):
        self.model_name = model_name
        self.dimensions = dimensions
        self._client = None
        self._encoder = None

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not isinstance(texts, list) or any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError("Embedding input must be a list of nonempty strings")
        if not texts:
            return []
        if not os.environ.get("OPENAI_API_KEY"):
            raise MarketRAGProviderError("Set OPENAI_API_KEY before building or searching the index")

        try:
            if self._encoder is None:
                import tiktoken

                self._encoder = tiktoken.encoding_for_model(self.model_name)
            token_lengths = [
                len(self._encoder.encode(text, disallowed_special=())) for text in texts
            ]
        except Exception as exc:
            raise MarketRAGProviderError(
                f"Tokenizer initialization failed ({type(exc).__name__})"
            ) from exc
        if any(length > 8_192 for length in token_lengths):
            raise ValueError("Embedding input exceeds 8192 tokens")

        try:
            if self._client is None:
                from openai import OpenAI

                self._client = OpenAI(timeout=30.0, max_retries=2)

            vectors: list[list[float]] = []
            for start in range(0, len(texts), 16):
                batch = texts[start : start + 16]
                response = self._client.embeddings.create(
                    model=self.model_name,
                    dimensions=self.dimensions,
                    input=batch,
                    encoding_format="float",
                )
                ordered = sorted(response.data, key=lambda item: item.index)
                if [item.index for item in ordered] != list(range(len(batch))):
                    raise MarketRAGProviderError("Embedding response has invalid indices")
                vectors.extend(item.embedding for item in ordered)
        except MarketRAGProviderError:
            raise
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            raise MarketRAGProviderError(
                f"OpenAI embedding failed ({type(exc).__name__}, status={status})"
            ) from exc

        return _validate_vectors(
            vectors,
            expected_count=len(texts),
            dimensions=self.dimensions,
        )

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


# ---------------------------------------------------------------------------
# 공개 MarketRAG 클래스
# ---------------------------------------------------------------------------


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
            raise ValueError("Injected embedding settings must match MarketRAG settings")

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
            self._embeddings = _OpenAIEmbeddings(self.model_name, self.dimensions)
        try:
            vectors = self._embeddings.embed_documents(texts)
        except (MarketRAGProviderError, ValueError):
            raise
        except Exception as exc:
            raise MarketRAGProviderError(
                f"Embedding backend failed ({type(exc).__name__})"
            ) from exc
        return _validate_vectors(
            vectors,
            expected_count=len(texts),
            dimensions=self.dimensions,
        )

    def _embed_query(self, query: str) -> list[float]:
        if self._embeddings is None:
            self._embeddings = _OpenAIEmbeddings(self.model_name, self.dimensions)
        try:
            vector = self._embeddings.embed_query(query)
        except (MarketRAGProviderError, ValueError):
            raise
        except Exception as exc:
            raise MarketRAGProviderError(
                f"Embedding backend failed ({type(exc).__name__})"
            ) from exc
        return _validate_vectors(
            [vector],
            expected_count=1,
            dimensions=self.dimensions,
        )[0]

    def prepare_chunks(self) -> list[MarketChunk]:
        """모든 PDF를 읽어 페이지 인용이 가능한 청크 목록을 만듭니다.

        이 메서드는 OpenAI와 Qdrant를 호출하지 않으므로 데이터 준비 상태를
        오프라인에서 확인하거나 테스트할 때도 사용할 수 있습니다.
        """

        with self._lock:
            self._ensure_open()
            chunks: list[MarketChunk] = []

            for source in self._sources:
                pdf_path = self.pdf_dir / source.file_name
                try:
                    reader = PdfReader(str(pdf_path))
                except Exception as exc:
                    raise MarketRAGDataError(f"Cannot read PDF {pdf_path}: {exc}") from exc

                for page_number, page in enumerate(reader.pages, start=1):
                    try:
                        page_text = _clean_page_text(page.extract_text() or "")
                    except Exception as exc:
                        raise MarketRAGDataError(
                            f"Cannot extract {source.file_name} page {page_number}: {exc}"
                        ) from exc

                    # 표지만 이미지인 문서 등은 빈 페이지일 수 있습니다. 검색에
                    # 사용할 텍스트가 없으면 청크를 만들지 않습니다.
                    for chunk_index, text in enumerate(_chunk_page_text(page_text)):
                        stable_key = (
                            f"market-rag:{source.document_id}:{page_number}:"
                            f"{chunk_index}:{text}"
                        )
                        chunks.append(
                            MarketChunk(
                                chunk_id=str(uuid5(NAMESPACE_URL, stable_key)),
                                document_id=source.document_id,
                                title=source.title,
                                publisher=source.publisher,
                                published_year=source.published_year,
                                source_url=source.source_url,
                                source_file=source.file_name,
                                page=page_number,
                                chunk_index=chunk_index,
                                region=source.region,
                                language=source.language,
                                market_segments=source.market_segments,
                                text=text,
                            )
                        )

            if not chunks:
                raise MarketRAGDataError("No searchable text was extracted from the PDF directory")
            return chunks

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
    def _validate_optional_filter(name: str, values: list[str] | None) -> list[str] | None:
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
                    raise MarketRAGStoreError("Qdrant returned a nonfinite similarity score")
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
