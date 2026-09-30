"""기업 조회 조건·반환값·오류 타입입니다."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from pydantic import BaseModel

Scalar = str | int | float | None


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
