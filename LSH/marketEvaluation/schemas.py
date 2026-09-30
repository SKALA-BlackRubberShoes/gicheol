"""시장성 평가 단계 사이에서 주고받는 데이터 구조입니다.

문자열 딕셔너리를 자유롭게 넘기지 않고 Pydantic 모델을 사용하면, 팀원이
노드를 연결할 때 필드명 오타와 1~5점 범위 오류를 바로 발견할 수 있습니다.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


CriterionName = Literal[
    "customer_willingness",
    "market_size",
    "growth_timing",
    "adoption_feasibility",
    "scalability",
]


class MarketResearchPlan(BaseModel):
    """기업정보를 읽은 뒤 LLM이 만드는 검색 계획입니다."""

    model_config = ConfigDict(extra="forbid")

    market_definition: str = Field(min_length=10)
    pdf_queries: list[str] = Field(min_length=3, max_length=8)
    web_queries: list[str] = Field(min_length=3, max_length=8)


class EvidenceSource(BaseModel):
    """에이전트가 평가에 사용할 수 있도록 정규화한 PDF·웹 출처입니다."""

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(min_length=1)
    source_type: Literal["pdf", "web"]
    title: str = Field(min_length=1)
    publisher: str | None = None
    url: str | None = None
    page: int | None = Field(default=None, ge=1)
    published_at: str | None = None
    excerpt: str = Field(min_length=1)


class CriterionResult(BaseModel):
    """핵심 평가표의 한 항목에 대한 LLM 판단입니다."""

    model_config = ConfigDict(extra="forbid")

    criterion: CriterionName
    score: int = Field(ge=1, le=5)
    reason: str = Field(min_length=1)
    source_ids: list[str] = Field(default_factory=list)


class MarketEvaluationDraft(BaseModel):
    """LLM이 반환하는 초안입니다. 가중점수는 아직 포함하지 않습니다."""

    model_config = ConfigDict(extra="forbid")

    criteria: list[CriterionResult] = Field(min_length=5, max_length=5)
    market_risks: list[str] = Field(default_factory=list)


class MarketEvaluationResult(BaseModel):
    """투자 판단 노드에 전달할 최종 시장성 평가 결과입니다."""

    model_config = ConfigDict(extra="forbid")

    company_id: str
    company_name: str
    market_definition: str
    criteria: list[CriterionResult]
    evidence: list[EvidenceSource]
    market_score_100: float = Field(ge=0, le=100)
    investment_score_25: float = Field(ge=0, le=25)
    market_risks: list[str] = Field(default_factory=list)


class WebSearchResult(BaseModel):
    """웹 검색 제공자와 에이전트 사이의 공통 반환 형식입니다."""

    model_config = ConfigDict(extra="forbid")

    query: str
    title: str
    url: str
    publisher: str | None = None
    published_at: str | None = None
    content: str = Field(min_length=1)

