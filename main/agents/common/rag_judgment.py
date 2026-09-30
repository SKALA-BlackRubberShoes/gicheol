"""Validate LLM document assessments without assigning fixed company ratings."""
from copy import deepcopy

from pydantic import BaseModel, Field, field_validator

from main.agents.common.evidence import validate_evidence_ids, source_metadata


class RagJudgmentScore(BaseModel):
    criterion: str
    rating: int = Field(ge=0, le=5, strict=True)
    rationale: str = Field(min_length=1)
    evidence_ids: list[str]
    limitations: str = Field(min_length=1, description="What the cited documents cannot establish")

    @field_validator("rating", mode="before")
    @classmethod
    def missing_rating_is_zero(cls, value):
        return 0 if value is None else value


def validate_rag_judgment(scores, records, company, expected, strict_score):
    if not scores:
        if any(record.document_id for record in records):
            raise ValueError("PDF evidence requires a RAG assessment for each criterion")
        return []
    if len(scores) != len(expected) or {item.criterion for item in scores} != set(expected):
        raise ValueError("RAG scores must contain every criterion exactly once")
    by_id = {record.id: record for record in records}
    qualified = {item["criterion"]: item for item in strict_score["criteria"]}
    result = []
    for name in expected:
        item = next(score for score in scores if score.criterion == name)
        if not item.rationale.strip() or not item.limitations.strip():
            raise ValueError("RAG rating needs a rationale and limitations")
        cited = validate_evidence_ids(item.evidence_ids, by_id, name, item.rating > 0, unique=True)
        if any(record.evidence_scope not in {"company_technical", "company_description"} for record in cited):
            raise ValueError("Background or investment documents cannot support technical/competition ratings")
        target = company.casefold()
        owners = {record.company.casefold() for record in cited}
        pdf_owners = {record.company.casefold() for record in cited if record.document_id}
        if item.rating > 0:
            if target not in pdf_owners:
                raise ValueError(f"{name} requires target-company PDF evidence")
            if name in {"differentiation", "comparable_performance"}:
                if not pdf_owners - {target}:
                    raise ValueError(f"{name} requires both companies' PDF evidence")
            elif owners != {target}:
                raise ValueError(f"{name} must use target-company evidence only")
        rating = item.rating
        reason = item.rationale.strip()
        # Preliminary document judgment cannot bypass the existing measurement gates.
        if name in {"performance_validation", "comparable_performance"}:
            rating = min(rating, qualified[name]["rating"])
            if rating != item.rating:
                reason += " 조건을 갖춘 성능 검증 점수를 넘을 수 없어 조정했다."
        result.append({
            "criterion": name, "rating": rating, "points": rating * 5,
            "rationale": reason, "evidence_ids": item.evidence_ids,
            "limitations": item.limitations, "basis": "llm_pdf_assessment",
        })
    return result


def merge_rag_judgment(strict_score, csv_score, rag_criteria):
    """Prefer qualified findings, then PDF judgment, then the CSV fallback.

    A documented PDF zero also overrides a positive CSV fallback; selecting
    whichever value is larger would reward cherry-picking weaker evidence.
    """
    if not rag_criteria:
        return csv_score
    by_rag = {item["criterion"]: item for item in rag_criteria}
    by_csv = {item["criterion"]: item for item in csv_score["criteria"]}
    selected = []
    for item in strict_score["criteria"]:
        candidate = by_rag[item["criterion"]]
        if item["rating"] > 0:
            selected.append({**item, "basis": "qualified_evidence"})
        elif candidate["evidence_ids"]:
            selected.append(deepcopy(candidate))
        else:
            fallback = deepcopy(by_csv[item["criterion"]])
            fallback.setdefault("basis", "unscored")
            selected.append(fallback)
    result = deepcopy(strict_score)
    result.update({
        "criteria": selected,
        "total": sum(item["points"] for item in selected),
        "score_basis": "qualified_evidence_and_llm_pdf_assessment",
        "verified_score": deepcopy(strict_score),
        "status": "provisional" if any(item["basis"] in {"llm_pdf_assessment", "llm_csv_assessment"} for item in selected) else strict_score["status"],
        "caveat": "PDF와 CSV에 근거한 모델 판단이다. verified_score는 기존 필수 근거 조건을 충족한 점수이며 독립기관 인증을 뜻하지 않는다.",
    })
    return result


def complete_sources(sources, records, scorecard):
    cited = {ref for item in scorecard["criteria"] for ref in item["evidence_ids"]}
    result = {item["id"]: item for item in sources}
    for item in source_metadata([record for record in records if record.id in cited], include_company=True):
        result[item["id"]] = item
    return list(result.values())
