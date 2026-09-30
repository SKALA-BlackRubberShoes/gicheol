"""투자 판단 입력·출력 State와 근거의 데이터 계약입니다."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, NotRequired, TypedDict

class Evidence(TypedDict):
    id: str
    document_id: str
    chunk_id: str
    company_id: str | None
    title: str
    publisher: str
    url: str
    published_at: str | None
    data_year: int | None
    collected_at: str
    page: int | None
    excerpt: str
    source_type: Literal["company", "customer", "official", "independent"]
    is_mock: NotRequired[bool]


class InvestmentJudgeState(TypedDict, total=False):
    current_candidate: dict[str, Any]
    technology_analysis: dict[str, Any] | None
    market_analysis: dict[str, Any] | None
    competition_analysis: dict[str, Any] | None
    evidence_registry: dict[str, Evidence]
    team_rating: dict[str, Any] | None
    team_evidence: list[Evidence]
    team_missing_items: list[str]
    team_conflicts: list[str]
    judgment_notes: list[str]
    commitment_note: str
    market_definition: str | None
    as_of_date: str
    retry_count: int
    team_research_log: list[dict[str, Any]]
    decision_policy: dict[str, Any]
    decision: str
    current_evaluation: dict[str, Any]
    scorecard: dict[str, Any]
    total_score: float | None
    upstream_score_90: float | None
    judge_score_10: float | None
    decision_reasons: list[str]
    missing_items: list[str]
    conflicts: list[str]
    used_reference_ids: list[str]
    needs_research: bool
    research_requests: list[dict[str, str]]
    retry_available: bool
    updated_candidate: dict[str, Any]
    next_action: Literal["report", "hold"]
    report_payload: dict[str, Any] | None
    hold_payload: dict[str, Any] | None


TeamResearcher = Callable[[dict[str, Any]], dict[str, Any]]

