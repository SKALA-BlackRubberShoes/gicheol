"""
기업 정보 조회·검색 RAG 파이프라인

임베딩 모델: OpenAI text-embedding-3-small (기본 512차원)
벡터스토어:  Qdrant (http://localhost:6333)
CSV 데이터: docs/data/base/raw/thevc_startups_30_updated.csv

회사 한 행을 문서 하나로 사용합니다.
ID·항목 조회는 메모리의 CSV 데이터에서, 의미 검색은 Qdrant에서 처리합니다.
기업 선택과 LangGraph State 관리는 호출하는 에이전트가 담당합니다.
"""
from __future__ import annotations

import csv
import math
import operator
import os
import re
from datetime import date
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from threading import RLock
from typing import Literal, Protocol
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel

# 공용 컬렉션명과 기본 CSV 경로
COLLECTION_NAME = "companies_small_512"
DEFAULT_CSV_PATH = Path(__file__).resolve().parents[2] / "docs/data/base/raw/thevc_startups_30_updated.csv"
Scalar = str | int | float | None


# ──────────────────────────────────────────
# 오류와 반환 데이터 구조
# ──────────────────────────────────────────

class RAGDataError(ValueError):
    """CSV 형식·값 또는 회사 ID에 문제가 있을 때 발생합니다."""


class RAGCompanyNotFoundError(LookupError):
    """항목을 조회할 회사 ID가 없을 때 발생합니다."""


class RAGUnknownFieldError(LookupError):
    """요청한 원문 컬럼 또는 정규화 항목이 없을 때 발생합니다."""


class RAGProviderError(RuntimeError):
    """OpenAI 호출 실패 또는 잘못된 임베딩 응답을 알립니다."""


class RAGStoreError(RuntimeError):
    """Qdrant 연결·저장·검색 오류를 알립니다."""


class RAGIndexNotReady(RuntimeError):
    """의미 검색 전에 build_index()가 완료되지 않았을 때 발생합니다."""


class RAGClosedError(RuntimeError):
    """close()로 종료한 객체를 다시 사용할 때 발생합니다."""


class SourceRef(BaseModel):
    """근거가 된 CSV 경로와 레코드 번호입니다. 첫 기업의 번호는 2입니다."""

    csv_path: str
    record_number: int


class CompanyRecord(BaseModel):
    """회사 한 행의 원문(raw), 정규화 값(values), 검색 본문(content)입니다."""

    company_id: str
    company_name: str
    raw: dict[str, str]
    values: dict[str, Scalar]
    content: str
    source: SourceRef


@dataclass
class CompanyFilter:
    """정규화 항목에 적용할 조건입니다. 여러 조건은 AND로 결합합니다."""

    field: str
    op: Literal["eq", "ne", "gt", "gte", "lt", "lte", "is_null", "not_null"]
    value: Scalar = None


class SearchHit(BaseModel):
    """검색된 회사와 코사인 유사도입니다. score는 투자 점수가 아닙니다."""

    company: CompanyRecord
    score: float


class EmbeddingBackend(Protocol):
    """OpenAI 임베더와 테스트용 임베더가 공통으로 제공하는 인터페이스입니다."""

    model_name: str
    dimensions: int

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


# ──────────────────────────────────────────
# CSV 로딩과 정규화
# ──────────────────────────────────────────

# 조회용 영문 키 → (CSV 컬럼명, 변환할 자료형)
_FIELDS = {
    "company_id": ("company_id", "str"), "company_name": ("기업명", "str"),
    "location": ("소재지역", "str"), "sector": ("분야", "str"),
    "subsector": ("소분야", "str"), "technology": ("기술", "str"),
    "product_type": ("제품 형태", "str"), "funding_stage": ("투자 유치 단계 (최근)", "str"),
    "funding_latest_won": ("투자 유치 금액 (최근)", "money"),
    "funding_total_won": ("투자 유치 금액 (누적)", "money"),
    "employees": ("임직원 수", "int"), "employees_change": ("1개월전 대비 임직원 수", "int"),
    "patent_count": ("특허 수", "int"), "funding_latest_date": ("투자 유치일 (최근)", "date"),
    "company_age_years": ("업력", "age"),
}
# 의미 검색에는 회사·제품·서비스 설명을 사용하고, 수치는 values에 보관합니다.
_CONTENT_COLUMNS = ("기업명", "대표제품", "서비스", "분야", "소분야", "기술", "제품 형태")
_REQUIRED_COLUMNS = {c for c, _ in _FIELDS.values()} | set(_CONTENT_COLUMNS) | {
    "홈페이지", "투자자 분류", "인증/자격"}
_COMPARISONS = {"eq": operator.eq, "ne": operator.ne, "gt": operator.gt,
                "gte": operator.ge, "lt": operator.lt, "lte": operator.le}
_NUMBER = r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"


def _iso_date(value: str) -> str:
    """YYYY-MM-DD 형식과 실제로 존재하는 날짜인지 확인합니다."""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("expected YYYY-MM-DD")
    return date.fromisoformat(value).isoformat()


def _normalize(value: str, kind: str) -> Scalar:
    """CSV 문자열을 조회용 값으로 변환합니다. 빈 값·NULL은 None으로 유지합니다."""
    value = value.strip()
    if not value or value == "NULL":
        return None
    if kind == "str":
        return value
    if kind == "date":
        return _iso_date(value)
    if kind == "int":
        if not re.fullmatch(r"[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)", value):
            raise ValueError("expected an integer")
        return int(value.replace(",", ""))
    if kind == "money":
        match = re.fullmatch(rf"({_NUMBER})\s*(억원|만원|원)", value)
        if not match:
            raise ValueError("expected a KRW amount with 원, 만원, or 억원")
        # 원·만원·억원을 원 단위 정수로 통일합니다.
        amount = Decimal(match[1].replace(",", "")) * {"원": 1, "만원": 10_000, "억원": 100_000_000}[match[2]]
        if amount != amount.to_integral_value():
            raise ValueError("amount must be a whole number of won")
        return int(amount)
    if not re.fullmatch(r"\d+(?:\.\d+)?년차", value):
        raise ValueError("expected years followed by 년차")
    result = float(value[:-2])
    if not math.isfinite(result):
        raise ValueError("years must be finite")
    return result


def _load_csv(path: Path) -> dict[str, CompanyRecord]:
    """회사 ID를 키로 CSV를 읽습니다. 행 순서로 ID를 새로 만들지 않습니다."""
    records = {}
    try:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream, strict=True)
            headers = reader.fieldnames or []
            missing = _REQUIRED_COLUMNS - set(headers)
            if missing or len(headers) != len(set(headers)):
                raise RAGDataError(f"{path}: record 1: missing/duplicate columns: {sorted(missing)}")
            for number, raw in enumerate(reader, start=2):
                if None in raw or any(v is None for v in raw.values()):
                    raise RAGDataError(f"{path}: record {number}: incorrect column count")
                values = {}
                for key, (column, kind) in _FIELDS.items():
                    try:
                        values[key] = _normalize(raw[column], kind)
                    except (ValueError, ArithmeticError) as exc:
                        raise RAGDataError(f"{path}: record {number}, column {column}: {exc}") from exc
                # 공백을 제거한 뒤에도 ID가 비어 있거나 중복이면 적재를 중단합니다.
                company_id = values["company_id"]
                if company_id is None or company_id in records:
                    raise RAGDataError(f"{path}: record {number}, column company_id: blank/NULL or duplicate ID {company_id!r}")
                if values["company_name"] is None:
                    raise RAGDataError(f"{path}: record {number}, column 기업명: required")
                content = "\n".join(f"{c}: {raw[c].strip()}" for c in _CONTENT_COLUMNS
                                    if raw[c].strip() not in ("", "NULL"))
                records[company_id] = CompanyRecord(
                    company_id=company_id, company_name=values["company_name"], raw=raw,
                    values=values, content=content,
                    source=SourceRef(csv_path=str(path), record_number=number))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise RAGDataError(f"{path}: cannot read CSV: {exc}") from exc
    return records


# ──────────────────────────────────────────
# 조건 검색
# ──────────────────────────────────────────

def _validate_filters(filters: list[CompanyFilter] | None) -> list[CompanyFilter]:
    """필터 항목·연산자·값의 자료형이 비교 가능한 조합인지 확인합니다."""
    if filters is None:
        return []
    if not isinstance(filters, list) or any(not isinstance(f, CompanyFilter) for f in filters):
        raise ValueError("filters must be a list of CompanyFilter")
    for f in filters:
        if f.field not in _FIELDS or f.op not in {*_COMPARISONS, "is_null", "not_null"}:
            raise ValueError(f"Unsupported filter: {f.field} {f.op}")
        kind = _FIELDS[f.field][1]
        if f.op in ("is_null", "not_null"):
            if f.value is not None:
                raise ValueError("Null filters take no value")
            continue
        if kind in ("str", "date"):
            valid = isinstance(f.value, str)
        else:
            valid = type(f.value) in (int, float) and math.isfinite(f.value)
        if not valid:
            raise ValueError(f"Invalid value type for {f.field}")
        if kind == "date":
            _iso_date(f.value)
        if kind == "str" and f.op not in ("eq", "ne"):
            raise ValueError("Range comparisons require numeric or date fields")
    return filters


def _matches(record: CompanyRecord, filters: list[CompanyFilter]) -> bool:
    """회사가 모든 조건을 만족하는지 확인합니다. 결측은 일반 비교에서 제외합니다."""
    for f in filters:
        value = record.values[f.field]
        if f.op == "is_null":
            ok = value is None
        elif f.op == "not_null":
            ok = value is not None
        else:
            ok = value is not None and _COMPARISONS[f.op](value, f.value)
        if not ok:
            return False
    return True


def _validate_id(company_id: str) -> str:
    """조회할 ID의 앞뒤 공백을 제거하고 빈 문자열을 거부합니다."""
    if not isinstance(company_id, str) or not company_id.strip():
        raise ValueError("company_id must be a nonempty string")
    return company_id.strip()


# ──────────────────────────────────────────
# OpenAI 임베딩
# ──────────────────────────────────────────

def _validate_vectors(vectors: list[list[float]], count: int, dimensions: int) -> list[list[float]]:
    """임베딩 개수·차원·수치가 Qdrant에 저장하고 검색할 수 있는 값인지 확인합니다."""
    if not isinstance(vectors, list) or len(vectors) != count:
        raise RAGProviderError("Embedding response count does not match input")
    for vector in vectors:
        if (not isinstance(vector, list) or len(vector) != dimensions
                or any(type(x) not in (int, float) or not math.isfinite(x) for x in vector)):
            raise RAGProviderError("Invalid embedding dimension or nonfinite/nonnumeric value")
        norm = math.hypot(*vector)
        if not math.isfinite(norm) or norm == 0:
            raise RAGProviderError("Embedding must have a finite, nonzero norm")
    return vectors


class _OpenAIEmbeddings:
    """문서와 질문을 같은 OpenAI 모델·차원으로 임베딩합니다."""

    def __init__(self, model_name: str, dimensions: int):
        self.model_name, self.dimensions = model_name, dimensions
        self._client = self._encoder = None

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """문서를 최대 16개씩 임베딩합니다. 길이 초과 문서를 자동으로 자르지 않습니다."""
        if not isinstance(texts, list) or any(not isinstance(t, str) or not t.strip() for t in texts):
            raise ValueError("Embedding input must be a list of nonempty strings")
        if not texts:
            return []
        if not os.environ.get("OPENAI_API_KEY"):
            raise RAGProviderError("Set OPENAI_API_KEY before embedding documents or queries")
        try:
            if self._encoder is None:
                import tiktoken
                self._encoder = tiktoken.encoding_for_model(self.model_name)
            lengths = [len(self._encoder.encode(t, disallowed_special=())) for t in texts]
        except Exception as exc:
            raise RAGProviderError(f"Tokenizer initialization failed ({type(exc).__name__})") from exc
        if any(n > 8192 for n in lengths):
            raise ValueError("Embedding input exceeds 8192 tokens; shorten it explicitly")
        vectors = []
        try:
            if self._client is None:
                from openai import OpenAI
                # 실제 임베딩 시점에만 환경변수의 API 키로 클라이언트를 만듭니다.
                self._client = OpenAI(timeout=30.0, max_retries=2)
            for start in range(0, len(texts), 16):
                batch = texts[start:start + 16]
                response = self._client.embeddings.create(
                    model=self.model_name, dimensions=self.dimensions,
                    input=batch, encoding_format="float")
                items = response.data
                indices = [item.index for item in items]
                if (any(type(i) is not int for i in indices)
                        or sorted(indices) != list(range(len(batch)))):
                    raise RAGProviderError("Embedding response has missing/duplicate/invalid indices")
                # 응답 순서가 달라도 입력 문서와 벡터의 대응을 유지합니다.
                ordered = [item.embedding for item in sorted(items, key=lambda item: item.index)]
                vectors.extend(_validate_vectors(ordered, len(batch), self.dimensions))
        except RAGProviderError:
            raise
        except Exception as exc:
            # 외부로 전달하는 오류 메시지에는 요청 본문이나 API 키를 넣지 않습니다.
            status = getattr(exc, "status_code", None)
            raise RAGProviderError(f"OpenAI embedding failed ({type(exc).__name__}, status={status})") from exc
        return vectors

    def embed_query(self, text: str) -> list[float]:
        """질문 하나를 문서와 동일한 설정으로 임베딩합니다."""
        return self.embed_documents([text])[0]

    def close(self) -> None:
        """이 임베더가 생성한 OpenAI 클라이언트를 닫습니다."""
        if self._client is not None:
            self._client.close()
            self._client = None


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

    def __init__(self, csv_path: str | Path = DEFAULT_CSV_PATH, *, model_name: str = "text-embedding-3-small",
                 dimensions: int = 512, qdrant_url: str = "http://localhost:6333",
                 embeddings: EmbeddingBackend | None = None):
        if model_name != "text-embedding-3-small":
            raise ValueError("Only text-embedding-3-small is supported in this version")
        if type(dimensions) is not int or not 1 <= dimensions <= 1536:
            raise ValueError("dimensions must be an integer from 1 to 1536")
        if embeddings is not None and (embeddings.model_name != model_name or embeddings.dimensions != dimensions):
            raise ValueError("Injected embedding model/dimensions must match the configuration")
        self.csv_path = Path(csv_path).expanduser().resolve()
        self.model_name, self.dimensions, self.qdrant_url = model_name, dimensions, qdrant_url
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

    def get_fields(self, company_id: str, fields: list[str], *, raw: bool = False) -> dict[str, Scalar]:
        """요청한 항목만 순서대로 반환합니다. 없는 회사·항목은 실제 결측과 구분합니다."""
        with self._lock:
            self._ensure_open()
            if (not isinstance(fields, list) or not fields
                    or any(not isinstance(f, str) or not f.strip() for f in fields)
                    or len(fields) != len(set(fields)) or type(raw) is not bool):
                raise ValueError("fields must be a nonempty list of unique strings; raw must be bool")
            record = self._records.get(_validate_id(company_id))
            if record is None:
                raise RAGCompanyNotFoundError(f"Unknown company_id: {company_id}")
            values = record.raw if raw else record.values
            unknown = [f for f in fields if f not in values]
            if unknown:
                raise RAGUnknownFieldError(f"Unknown {'raw' if raw else 'normalized'} fields: {unknown}")
            return {f: values[f] for f in fields}

    def list_companies(self, *, filters: list[CompanyFilter] | None = None) -> list[CompanyRecord]:
        """조건을 만족하는 전체 회사를 ID 사전순으로 반환합니다. API 호출은 없습니다."""
        with self._lock:
            self._ensure_open()
            filters = _validate_filters(filters)
            return [r.model_copy(deep=True) for _, r in sorted(self._records.items()) if _matches(r, filters)]

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
            raise RAGStoreError(f"Qdrant {method} failed ({type(exc).__name__}); check the server connection") from exc

    def _embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        """설정된 임베더로 문서 또는 질문 벡터를 생성합니다."""
        if not texts:
            return []
        if self._embeddings is None:
            self._embeddings = _OpenAIEmbeddings(self.model_name, self.dimensions)
        try:
            vectors = ([self._embeddings.embed_query(texts[0])] if query
                       else self._embeddings.embed_documents(texts))
            return _validate_vectors(vectors, len(texts), self.dimensions)
        except (RAGProviderError, ValueError):
            raise
        except Exception as exc:
            raise RAGProviderError(f"Embedding backend failed ({type(exc).__name__})") from exc

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
            exists = self._store_call("collection_exists", collection_name=COLLECTION_NAME)
            if exists and not rebuild:
                self._ready = False
                vectors = self._store_call("get_collection", collection_name=COLLECTION_NAME).config.params.vectors
                if getattr(vectors, "size", None) != self.dimensions:
                    raise ValueError("Collection dimension differs; run build_index(rebuild=True)")
                count = self._store_call("count", collection_name=COLLECTION_NAME, exact=True).count
                self._ready = True
                return count

            # CSV 검증과 임베딩을 먼저 마칩니다. 여기서 실패하면 기존 색인을 유지합니다.
            records = _load_csv(self.csv_path) if rebuild else self._records
            ordered = [r for _, r in sorted(records.items())]
            vectors = self._embed([r.content for r in ordered])
            from qdrant_client import models
            # 회사 ID에서 UUID를 만들므로 CSV 행 순서가 바뀌어도 point ID가 유지됩니다.
            points = [models.PointStruct(
                id=str(uuid5(NAMESPACE_URL, "basic-rag:" + r.company_id)), vector=vector,
                payload={"company_id": r.company_id, "content": r.content,
                         "raw": r.raw, "values": r.values}) for r, vector in zip(ordered, vectors)]
            self._ready = False
            try:
                if exists:
                    self._store_call("delete_collection", collection_name=COLLECTION_NAME)
                self._store_call("create_collection", collection_name=COLLECTION_NAME,
                                 vectors_config=models.VectorParams(size=self.dimensions, distance=models.Distance.COSINE))
                if points:
                    self._store_call("upsert", collection_name=COLLECTION_NAME, points=points, wait=True)
                count = self._store_call("count", collection_name=COLLECTION_NAME, exact=True).count
            except RAGStoreError as exc:
                raise RAGStoreError("Index replacement failed; run build_index(rebuild=True) again") from exc
            self._records, self._ready = records, True
            return count

    def retrieve(self, query: str, n_results: int = 5, *, company_id: str | None = None,
                 filters: list[CompanyFilter] | None = None,
                 exclude_company_id: str | None = None) -> list[SearchHit]:
        """
        질문과 유사한 기업 후보를 반환합니다. build_index() 후 호출합니다.
        전체 CSV에 조건을 먼저 적용하고, 해당 회사들만 Qdrant에서 검색합니다.
        """
        with self._lock:
            self._ensure_open()
            if not self._ready:
                raise RAGIndexNotReady("Call build_index() before retrieve()")
            if not isinstance(query, str) or not query.strip() or type(n_results) is not int or n_results < 1:
                raise ValueError("query must be nonempty and n_results a positive integer")
            filters = _validate_filters(filters)
            company_id = _validate_id(company_id) if company_id is not None else None
            exclude_company_id = _validate_id(exclude_company_id) if exclude_company_id is not None else None
            eligible = [r.company_id for r in self._records.values()
                        if (company_id is None or r.company_id == company_id)
                        and r.company_id != exclude_company_id and _matches(r, filters)]
            if not eligible:
                return []
            vector = self._embed([query], query=True)[0]
            from qdrant_client import models
            response = self._store_call(
                "query_points", collection_name=COLLECTION_NAME, query=vector,
                query_filter=models.Filter(must=[models.FieldCondition(
                    key="company_id", match=models.MatchAny(any=eligible))]),
                limit=len(eligible), with_payload=True, with_vectors=False)
            hits = []
            allowed = set(eligible)
            for point in response.points:
                result_id = (point.payload or {}).get("company_id")
                if not isinstance(result_id, str) or result_id not in allowed:
                    raise RAGStoreError("Unknown/out-of-scope company_id in Qdrant; run build_index(rebuild=True)")
                if not math.isfinite(point.score):
                    raise RAGStoreError("Qdrant returned a nonfinite similarity score")
                # 서버에는 벡터를 묻고, 반환할 회사 정보는 현재 CSV 스냅샷에서 가져옵니다.
                hits.append(SearchHit(company=self._records[result_id].model_copy(deep=True), score=point.score))
            return sorted(hits, key=lambda hit: (-hit.score, hit.company.company_id))[:n_results]

    # ──────────────────────────────────────────
    # Public API — 상태 조회와 종료
    # ──────────────────────────────────────────

    @property
    def doc_count(self) -> int:
        """서버의 문서 수를 반환합니다. 준비 전·종료 후는 0이며 서버 장애는 오류로 알립니다."""
        with self._lock:
            if self._closed or not self._ready:
                return 0
            return self._store_call("count", collection_name=COLLECTION_NAME, exact=True).count

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
