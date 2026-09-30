"""기술 점수 계산과 각 평가항목의 근거 자격을 확인합니다."""

from __future__ import annotations

from typing import Any
from main.agents.common.evidence import (
    Evidence,
    validate_evidence_ids,
    source_metadata,
    data_label,
)
from .schemas import TechnologySummary


def _validate_citations(
    summary: TechnologySummary, records: list[Evidence], company: str
) -> tuple[list[dict[str, Any]], str]:
    """Check that every cited ID belongs to the searched company."""
    if summary.company.strip().casefold() != company.casefold():
        raise ValueError("The summary names a different company")

    by_id = {record.id: record for record in records}
    used: dict[str, Evidence] = {}

    def check(ids: list[str], label: str, required: bool) -> None:
        for record in validate_evidence_ids(ids, by_id, label, required):
            used[record.id] = record

    check(
        summary.summary_evidence_ids,
        "technology_summary",
        bool(summary.technology_summary.strip()),
    )
    problem = summary.problem_solution
    check(
        problem.evidence_ids,
        "problem_solution",
        any(
            (
                problem.problem.strip(),
                problem.product_approach.strip(),
                problem.observed_outcome.strip(),
                problem.customer_problem_quote.strip(),
                problem.baseline_or_goal_quote.strip(),
            )
        ),
    )
    for name in ("customer_problem_quote", "baseline_or_goal_quote"):
        quote = " ".join(getattr(problem, name).split())
        setattr(problem, name, quote)
        if quote and not any(
            quote in " ".join(by_id[evidence_id].text.split())
            for evidence_id in problem.evidence_ids
        ):
            raise ValueError(f"problem_solution.{name} is not an exact source excerpt")
    if not problem.customer_problem_quote:
        problem.problem = "고객이 직접 밝힌 문제는 제공된 자료에서 확인되지 않음"
        if problem.verdict == "supported":
            problem.verdict = "partial"
        problem.caveat += (
            " 고객 문제에 대한 직접 근거가 없어 해결 여부는 확정할 수 없다."
        )
    if not problem.baseline_or_goal_quote and problem.verdict == "supported":
        problem.verdict = "partial"
        problem.caveat += " 기존 방식의 성능 또는 고객 목표가 확인되지 않았다."
    if problem.verdict == "contested" and len(set(problem.evidence_ids)) < 2:
        raise ValueError("problem_solution needs at least two sources for a conflict")

    check(
        summary.ai_role.evidence_ids,
        "ai_role",
        summary.ai_role.status == "supported",
    )
    # 모델이 '자료 없음'을 성능 항목으로 만들면서 출처를 비우는 경우가 있다.
    # 출처 없는 성능 진술은 결과와 점수 양쪽에서 제외한다.
    attributed_validations = [
        validation for validation in summary.performance_validations
        if validation.evidence_ids
    ]
    if len(attributed_validations) != len(summary.performance_validations):
        summary.performance_validations = attributed_validations
        note = "출처가 없는 성능 진술을 제외함"
        if note not in summary.key_unknowns:
            summary.key_unknowns.append(note)
    for index, validation in enumerate(summary.performance_validations):
        check(validation.evidence_ids, f"performance_validations[{index}]", True)
        # A generated web summary or CSV introduction cannot prove measured performance.
        if not any(
            by_id[ref].evidence_scope == "company_technical"
            and not by_id[ref].is_derived
            for ref in validation.evidence_ids
        ):
            validation.measurement_basis = "unverified"
    check(summary.maturity_evidence_ids, "maturity", summary.maturity != "unknown")
    for index, claim in enumerate(summary.claims):
        label = f"claims[{index}]"
        check(claim.evidence_ids, label, claim.status != "insufficient")
        if claim.status == "contested" and len(set(claim.evidence_ids)) < 2:
            raise ValueError(f"{label} needs at least two sources for a conflict")
    for index, score in enumerate(summary.criterion_scores):
        check(
            score.evidence_ids,
            f"criterion_scores[{index}]",
            score.rating > 0,
        )

    if summary.maturity != "unknown" and not any(
        by_id[evidence_id].stage == summary.maturity
        for evidence_id in summary.maturity_evidence_ids
    ):
        raise ValueError("Maturity must be supported by matching source metadata")

    sources = source_metadata(used.values())
    return sources, data_label(sources)


def _build_scorecard(summary: TechnologySummary) -> dict[str, Any]:
    """Validate criterion provenance and derive points without LLM arithmetic."""
    expected = (
        "problem_solution",
        "ai_role",
        "performance_validation",
        "maturity",
    )
    scores = {score.criterion: score for score in summary.criterion_scores}
    if len(scores) != len(expected) or set(scores) != set(expected):
        raise ValueError(
            "criterion_scores must contain each technical criterion exactly once"
        )

    missing_conditions = {
        "",
        "자료 없음",
        "없음",
        "미상",
        "불명",
        "확인되지 않음",
        "unknown",
        "unspecified",
        "not provided",
        "n/a",
    }
    qualified_validations = [
        validation
        for validation in summary.performance_validations
        if validation.metric.strip()
        and validation.result.strip()
        and validation.measurement_basis in {"controlled_experiment", "field_measurement"}
        and validation.test_conditions.strip().casefold() not in missing_conditions
    ]
    section_ids = {
        "problem_solution": set(summary.problem_solution.evidence_ids),
        "ai_role": set(summary.ai_role.evidence_ids),
        "performance_validation": {
            evidence_id
            for validation in qualified_validations
            for evidence_id in validation.evidence_ids
        },
        "maturity": set(summary.maturity_evidence_ids),
    }
    unverified = {
        "problem_solution": (
            not summary.problem_solution.customer_problem_quote
            or summary.problem_solution.verdict != "supported"
        ),
        "ai_role": summary.ai_role.status != "supported",
        "performance_validation": (
            not qualified_validations
            or not set(scores["performance_validation"].evidence_ids)
            <= section_ids["performance_validation"]
        ),
        "maturity": summary.maturity == "unknown",
    }
    criteria: list[dict[str, Any]] = []
    for criterion in expected:
        score = scores[criterion]
        if not score.rationale.strip():
            raise ValueError(f"{criterion} needs a score rationale")
        if (
            criterion != "performance_validation"
            and score.rating > 0
            and not unverified[criterion]
            and not set(score.evidence_ids) <= section_ids[criterion]
        ):
            raise ValueError(f"{criterion} score cites evidence outside its assessment")
        rating = 0 if unverified[criterion] else score.rating
        rationale = score.rationale.strip()
        if rating == 0:
            rationale += " 필수 근거가 부족해 0점 처리했다."
        criteria.append(
            {
                "criterion": criterion,
                "rating": rating,
                "points": rating * 5,
                "rationale": rationale,
                "evidence_ids": score.evidence_ids,
            }
        )
    complete = all(item["rating"] > 0 for item in criteria)
    return {
        "criteria": criteria,
        "total": sum(item["points"] for item in criteria),
        "max": 100,
        "status": "scored" if complete else "insufficient_evidence",
    }
