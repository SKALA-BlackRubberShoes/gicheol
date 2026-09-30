"""보고서 입력 계약. 다른 에이전트가 전달한 점수/판정을 재계산하지 않는다."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Evidence(Contract):
    evidence_id: str = Field(min_length=1)
    title: str = "제목 미확인"
    kind: Literal["report", "paper", "web", "csv"] = "web"
    issuer: str = ""
    author: str = ""
    published_at: str = ""
    url: str = ""
    site_name: str = ""
    journal: str = ""
    volume: str = ""
    issue: str = ""
    pages: str = ""
    doc_id: str = ""
    chunk_id: str = ""
    company_id: str = ""
    page: int | str | None = None
    data_year: int | str | None = None
    collected_at: str = ""
    quote: str = ""
    # 상위 에이전트의 생성 요약은 원문 발췌(quote)와 분리해 보존한다.
    content_kind: Literal["source_excerpt", "generated_summary", "metadata_only", "unspecified"] = "unspecified"
    summary: str = ""
    locator: str = ""
    provenance_note: str = ""
    csv_path: str = ""
    record_number: int | None = Field(default=None, ge=2)
    # None은 자료 유형 미확인이다. False도 진위 검증 완료를 뜻하지 않는다.
    is_mock: bool | None = None

    @model_validator(mode="after")
    def separate_generated_content(self) -> "Evidence":
        if self.content_kind in {"generated_summary", "metadata_only"} and self.quote:
            raise ValueError("생성 요약·서지정보를 원문 quote로 저장할 수 없습니다.")
        return self


class AnalysisResult(Contract):
    status: Literal["completed", "insufficient", "failed"] = "insufficient"
    summary: str = ""
    details: dict[str, str] = Field(default_factory=dict)
    detail_evidence_ids: dict[str, list[str]] = Field(default_factory=dict)
    scores: dict[str, float | None] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    score_evidence_ids: dict[str, list[str]] = Field(default_factory=dict)
    strengths: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class InvestmentResult(Contract):
    status: Literal["completed", "insufficient", "failed"] = "insufficient"
    decision: Literal["추천", "보류", "미확인"] = "미확인"
    final_score: float | None = Field(default=None, ge=0, le=100)
    weighted_scores: dict[str, float | None] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    score_evidence_ids: dict[str, list[str]] = Field(default_factory=dict)
    risks: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class Eligibility(Contract):
    status: Literal["eligible", "ineligible", "unknown"] = "unknown"
    reason: str = "적격성 확인 결과가 전달되지 않았습니다."
    evidence: list[Evidence] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class CompanyEvaluation(Contract):
    company_id: str = Field(min_length=1)
    company_name: str = ""
    # 기존 BaseRAG.CompanyRecord의 model_dump()를 그대로 전달할 수 있다.
    company_record: dict[str, Any] | None = None
    eligibility: Eligibility = Field(default_factory=Eligibility)
    finance_data: dict[str, Any] = Field(default_factory=dict)
    technology_data: dict[str, Any] = Field(default_factory=dict)
    technical_result: AnalysisResult | None = None
    market_result: AnalysisResult | None = None
    competition_result: AnalysisResult | None = None
    team_result: AnalysisResult | None = None
    investment_result: InvestmentResult | None = None
    is_mock: bool = False


class ReportConfig(BaseModel):
    # 전체 그래프의 재검색 횟수/가중치 등은 보고서가 변경하지 않는다.
    model_config = ConfigDict(extra="allow")
    domain: str = "Physical AI / Robotics"
    as_of: str = "미확인"
    title: str = "AI 스타트업 투자 평가 보고서"
    is_mock: bool = False


class ReportInput(Contract):
    config: ReportConfig = Field(default_factory=ReportConfig)
    candidate_company_ids: list[str] = Field(default_factory=list)
    results_by_company: dict[str, CompanyEvaluation] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list)
    no_candidates_reason: str = ""
    source_payload: dict[str, Any] | None = None


class SummarySelection(Contract):
    """LLM은 원문 문장 ID만 선택한다. 점수/판정/사실을 새로 쓰지 않는다."""
    fact_ids: list[str] = Field(min_length=1, max_length=4)

    @field_validator("fact_ids")
    @classmethod
    def unique_ids(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("SUMMARY fact_ids must be unique")
        return values


def _plain(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _plain(value.model_dump())
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def normalize_state(state: dict[str, Any]) -> ReportInput:
    """누적 결과 또는 단일 기업 State를 읽고 입력을 변경하지 않는다.

    누적 결과 키가 있으면 빈 dict도 최종 결과로 존중한다. 이때 이전 루프의
    company_id를 다시 보고서에 넣지 않는다.
    """
    source = _plain(state)
    data = {key: source[key] for key in ReportInput.model_fields if key in source}
    if "results_by_company" not in source and source.get("company_id"):
        company = {k: source[k] for k in CompanyEvaluation.model_fields if k in source}
        data["results_by_company"] = {source["company_id"]: company}
    results = data.get("results_by_company", {})
    if not isinstance(results, dict):
        raise ValueError("results_by_company는 기업 ID를 키로 갖는 dict여야 합니다.")
    for company_id, value in results.items():
        if not isinstance(company_id, str) or not isinstance(value, dict):
            raise ValueError("기업 ID는 문자열, 기업별 결과는 dict여야 합니다.")
        value.setdefault("company_id", company_id)
        if value["company_id"] != company_id:
            raise ValueError(f"기업 ID 불일치: {company_id}")
        record = value.get("company_record") or {}
        if not isinstance(record, dict):
            raise ValueError(f"{company_id}: company_record는 dict 또는 CompanyRecord여야 합니다.")
        for field in ("raw", "values", "source"):
            if record.get(field) is not None and not isinstance(record[field], dict):
                raise ValueError(f"{company_id}: company_record.{field}는 dict여야 합니다.")
        if record.get("company_id", company_id) != company_id:
            raise ValueError(f"CompanyRecord의 기업 ID 불일치: {company_id}")
        if not value.get("company_name"):
            value["company_name"] = record.get("company_name") or company_id
    return ReportInput.model_validate(data)
