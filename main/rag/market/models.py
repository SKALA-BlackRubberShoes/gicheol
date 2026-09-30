"""시장 문서·청크·검색 결과와 오류 타입입니다."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


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
