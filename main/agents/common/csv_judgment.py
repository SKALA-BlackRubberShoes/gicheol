"""모델이 CSV 근거로 내린 점수를 검증하고 독립 검증 점수와 결합합니다.

CSV 값별 고정 점수 규칙은 두지 않습니다. 모델이 근거와 이유를 함께 제출합니다.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from main.rag.company import BaseRAG, CompanyFilter, CompanyRecord
from main.agents.common.evidence import CSV_EVIDENCE_COLUMNS, Evidence

TECHNOLOGY_CRITERIA = (
    "problem_solution", "ai_role", "performance_validation", "maturity"
)
COMPETITION_CRITERIA = (
    "differentiation", "comparable_performance", "defensibility", "adoption_risk"
)
_MISSING = {"", "null", "n/a", "없음", "해당 없음", "미상", "불명", "확인 불가"}


class CsvJudgmentScore(BaseModel):
    """LLM이 제안하는 CSV 기재 기반 항목 점수입니다."""

    criterion: str
    rating: int = Field(ge=0, le=5)
    rationale: str
    evidence_ids: list[str]
    source_fields: list[str]

    @field_validator("rating", mode="before")
    @classmethod
    def missing_rating_is_zero(cls, value: object) -> object:
        return 0 if value is None else value

    @model_validator(mode="after")
    def validate_reason(self) -> "CsvJudgmentScore":
        if not self.rationale.strip():
            raise ValueError("CSV 점수에는 구체적인 판단 이유가 필요합니다")
        if self.rating > 0 and (not self.evidence_ids or not self.source_fields):
            raise ValueError("양수 점수에는 CSV 근거 ID와 컬럼명이 필요합니다")
        return self


def find_unique_company_record(rag: BaseRAG, name: str) -> CompanyRecord | None:
    """회사명이 기본 CSV에서 유일하게 일치할 때만 모델 점수를 사용합니다."""
    if not isinstance(rag, BaseRAG):
        return None
    matches = rag.list_companies(filters=[CompanyFilter("company_name", "eq", name)])
    return matches[0] if len(matches) == 1 else None


def _filled(record: CompanyRecord, column: str) -> bool:
    value = record.raw.get(column)
    return isinstance(value, str) and value.strip().casefold() not in _MISSING


def validate_csv_judgment(
    scores: list[CsvJudgmentScore], companies: list[CompanyRecord],
    evidence: list[Evidence], expected: tuple[str, ...],
) -> list[dict[str, Any]]:
    """모델 점수의 컬럼·회사 소유권·성능 주장 자격을 확인합니다."""
    if len(scores) != len(expected) or {item.criterion for item in scores} != set(expected):
        raise ValueError("CSV 점수에는 각 평가 항목이 정확히 한 번씩 필요합니다")
    by_id = {f"CSV-{record.company_id}": record for record in companies}
    available_ids = {record.id for record in evidence}
    target_id = f"CSV-{companies[0].company_id}"
    validated = []
    for name in expected:
        item = next(score for score in scores if score.criterion == name)
        cited = set(item.evidence_ids)
        if len(cited) != len(item.evidence_ids) or not cited <= by_id.keys() & available_ids:
            raise ValueError(f"{name}: CSV 근거 ID가 없거나 중복됐습니다")
        if item.rating > 0:
            if target_id not in cited:
                raise ValueError(f"{name}: 대상 기업 CSV 근거가 필요합니다")
            if name in {"differentiation", "comparable_performance"} and len(cited) < 2:
                raise ValueError(f"{name}: 대상과 경쟁사 양쪽의 CSV 근거가 필요합니다")
            if name in {"defensibility", "adoption_risk"} and cited != {target_id}:
                raise ValueError(f"{name}: 대상 기업의 CSV 근거만 사용해야 합니다")
            for field in item.source_fields:
                if field not in CSV_EVIDENCE_COLUMNS or not any(
                    _filled(by_id[evidence_id], field) for evidence_id in cited
                ):
                    raise ValueError(f"{name}: 기재되지 않은 CSV 컬럼을 인용했습니다: {field}")
        rating = item.rating
        reason = item.rationale.strip()
        performance_fields = ("성능 지표", "시험 결과", "시험 조건")
        if name == "performance_validation" and rating > 0 and not all(
            _filled(companies[0], field) for field in performance_fields
        ):
            rating = 0
            reason += " CSV에 성능 지표·결과·조건이 모두 없어 성능 점수를 제외했다."
        same_conditions = all(_filled(companies[0], field) for field in performance_fields) and any(
            all(_filled(record, field) for field in performance_fields)
            and companies[0].raw["성능 지표"].strip().casefold()
            == record.raw["성능 지표"].strip().casefold()
            and companies[0].raw["시험 조건"].strip().casefold()
            == record.raw["시험 조건"].strip().casefold()
            for record in companies[1:]
        )
        if name == "comparable_performance" and rating > 0 and not same_conditions:
            rating = 0
            reason += " 양사의 동일 지표·시험 조건 성능 자료가 없어 비교 점수를 제외했다."
        validated.append({
            "criterion": name, "rating": rating, "points": rating * 5,
            "rationale": reason, "evidence_ids": item.evidence_ids,
            "source_fields": item.source_fields, "basis": "llm_csv_assessment",
        })
    return validated


def merge_csv_judgment(
    verified: dict[str, Any], csv_criteria: list[dict[str, Any]]
) -> dict[str, Any]:
    """독립 검증 점수가 0인 항목만 모델의 CSV 평가로 보완합니다."""
    csv_by_name = {item["criterion"]: item for item in csv_criteria}
    if {item["criterion"] for item in verified["criteria"]} != set(csv_by_name):
        raise ValueError("CSV and verified score criteria disagree")
    selected = []
    used_csv = False
    for item in verified["criteria"]:
        candidate = csv_by_name[item["criterion"]]
        if item["rating"] > 0 or candidate["rating"] == 0:
            selected.append({**item, "basis": "verified" if item["rating"] > 0 else "unscored"})
        else:
            selected.append(deepcopy(candidate))
            used_csv = True
    if not used_csv:
        return verified
    result = deepcopy(verified)
    result["criteria"] = selected
    result["total"] = sum(item["points"] for item in selected)
    result["status"] = "provisional"
    result["score_basis"] = "verified_and_llm_csv_assessment"
    result["verified_score"] = deepcopy(verified)
    result["caveat"] = "일부 점수는 모델이 CSV 기재를 해석한 잠정 판단입니다. 독립 실증·권리 검증 전입니다."
    return result
