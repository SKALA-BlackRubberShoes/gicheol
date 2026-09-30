"""지정 경쟁사 비교와 평가 결과 형식입니다."""

from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from main.agents.common.csv_judgment import CsvJudgmentScore
from main.agents.common.rag_judgment import RagJudgmentScore


class ComparisonCondition(BaseModel):
    dimension: Literal[
        "task", "metric", "protocol", "environment", "configuration", "stage"
    ]
    status: Literal["aligned", "different", "unverified"]
    reason: str = Field(description="Evidence for alignment or the missing condition")


class CompetitorAssessment(BaseModel):
    competitor: str
    verdict: Literal["clear", "unclear", "insufficient"]
    differentiation: str
    comparison_conditions: str
    like_for_like: bool
    condition_checks: list[ComparisonCondition] = Field(
        description="Task, metric, protocol, environment, configuration, and stage exactly once each"
    )
    target_evidence_ids: list[str]
    competitor_evidence_ids: list[str]
    caveat: str


class RiskAssessment(BaseModel):
    category: Literal["technical", "operational", "legal"]
    description: str
    status: Literal["documented", "potential", "insufficient"]
    evidence_ids: list[str] = Field(description="Target-company evidence only")
    caveat: str
    adoption_impact: str
    scaling_impact: str


class DefensibilityAssessment(BaseModel):
    statement: str
    status: Literal["supported", "insufficient"]
    evidence_ids: list[str] = Field(description="Target-company evidence only")
    caveat: str


SCORE_CRITERIA = (
    "differentiation",
    "comparable_performance",
    "defensibility",
    "adoption_risk",
)


class CriterionScore(BaseModel):
    """A rating is zero when the company evidence cannot support a score."""

    model_config = ConfigDict(strict=True)

    criterion: Literal[
        "differentiation", "comparable_performance", "defensibility", "adoption_risk"
    ]
    rating: int = Field(ge=0, le=5)
    rationale: str
    evidence_ids: list[str]

    @field_validator("rating", mode="before")
    @classmethod
    def missing_rating_is_zero(cls, value: object) -> object:
        return 0 if value is None else value

    @model_validator(mode="after")
    def validate_score(self) -> "CriterionScore":
        if not self.rationale.strip():
            raise ValueError(
                "A score needs a rationale, including when evidence is insufficient"
            )
        if self.rating > 0 and not self.evidence_ids:
            raise ValueError("A numeric rating needs supporting company evidence IDs")
        return self


class CompetitorComparison(BaseModel):
    company: str
    comparisons: list[CompetitorAssessment]
    risks: list[RiskAssessment]
    defensibility: DefensibilityAssessment
    criterion_scores: list[CriterionScore]
    key_unknowns: list[str]
    csv_assessment_scores: list[CsvJudgmentScore] = Field(min_length=4, max_length=4)
    rag_assessment_scores: list[RagJudgmentScore] = Field(default_factory=list)
