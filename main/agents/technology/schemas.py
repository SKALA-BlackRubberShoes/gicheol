"""기술 요약·평가 결과 형식입니다."""

from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field, field_validator
from main.agents.common.csv_judgment import CsvJudgmentScore
from main.agents.common.rag_judgment import RagJudgmentScore


class TechnologyClaim(BaseModel):
    category: Literal["mechanism", "performance", "limitation"]
    statement: str
    status: Literal["supported", "contested", "insufficient"]
    evidence_ids: list[str]
    caveat: str = Field(
        description="Test conditions, source conflict, or missing proof"
    )


class ProblemSolutionAssessment(BaseModel):
    problem: str = Field(
        description="Customer problem; say unverified without direct evidence"
    )
    product_approach: str
    observed_outcome: str = Field(description="Measured field outcome, or unverified")
    customer_problem_quote: str = Field(
        description="Short exact excerpt about customer pain; empty when unavailable"
    )
    baseline_or_goal_quote: str = Field(
        description="Short exact excerpt about the prior process or customer goal; empty when unavailable"
    )
    verdict: Literal["supported", "partial", "contested", "insufficient"]
    evidence_ids: list[str]
    caveat: str


class AIRoleAssessment(BaseModel):
    role: str = Field(
        description="What a learning-based AI model actually does, or unverified"
    )
    status: Literal["supported", "insufficient"]
    evidence_ids: list[str]
    caveat: str


class PerformanceValidation(BaseModel):
    metric: str
    result: str
    test_conditions: str = Field(
        description="Environment, duration or sample, and exclusions"
    )
    evidence_ids: list[str]
    caveat: str = Field(description="Limits on generalizing the result")
    measurement_basis: Literal["controlled_experiment", "field_measurement", "author_claim", "unverified"] = "unverified"


class TechnicalCriterionScore(BaseModel):
    criterion: Literal[
        "problem_solution", "ai_role", "performance_validation", "maturity"
    ]
    rating: int = Field(
        ge=0,
        le=5,
        description="Evidence-based rating from 1 to 5; 0 when evidence is insufficient",
    )
    rationale: str = Field(description="Why this rating or zero was assigned")
    evidence_ids: list[str] = Field(
        description="Company evidence IDs supporting the rating"
    )

    @field_validator("rating", mode="before")
    @classmethod
    def missing_rating_is_zero(cls, value: object) -> object:
        return 0 if value is None else value


class TechnologySummary(BaseModel):
    company: str
    technology_summary: str = Field(description="Two to four Korean sentences")
    summary_evidence_ids: list[str]
    problem_solution: ProblemSolutionAssessment
    ai_role: AIRoleAssessment
    performance_validations: list[PerformanceValidation]
    claims: list[TechnologyClaim]
    maturity: Literal["demo", "pilot", "commercial", "unknown"]
    maturity_evidence_ids: list[str]
    key_unknowns: list[str]
    criterion_scores: list[TechnicalCriterionScore] = Field(min_length=4, max_length=4)
    csv_assessment_scores: list[CsvJudgmentScore] = Field(min_length=4, max_length=4)
    rag_assessment_scores: list[RagJudgmentScore] = Field(default_factory=list)
