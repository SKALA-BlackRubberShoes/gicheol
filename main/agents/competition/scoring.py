"""경쟁 평가 구조·인용 검증과 점수 계산입니다."""

from __future__ import annotations

from typing import Any
from main.agents.common.evidence import Evidence, validate_evidence_ids, source_metadata
from .schemas import CompetitorComparison, SCORE_CRITERIA


def _validate_requested_structure(
    report: CompetitorComparison, company: str, competitors: list[str]
) -> None:
    if report.company.strip().casefold() != company.casefold():
        raise ValueError(
            f"Target company mismatch: expected {company!r}, got {report.company!r}"
        )
    expected = {name.casefold() for name in competitors}
    actual = [item.competitor.strip().casefold() for item in report.comparisons]
    if len(actual) != len(expected) or set(actual) != expected:
        raise ValueError(
            "Comparisons must cover each requested competitor exactly once"
        )
    categories = [risk.category for risk in report.risks]
    if len(categories) != 3 or set(categories) != {"technical", "operational", "legal"}:
        raise ValueError(
            "Risks must contain technical, operational, legal exactly once"
        )
    scored = [score.criterion for score in report.criterion_scores]
    if len(scored) != len(SCORE_CRITERIA) or set(scored) != set(SCORE_CRITERIA):
        raise ValueError("Scores must contain each of the four criteria exactly once")
    dimensions = {"task", "metric", "protocol", "environment", "configuration", "stage"}
    for item in report.comparisons:
        checks = item.condition_checks
        if len(checks) != 6 or {check.dimension for check in checks} != dimensions:
            raise ValueError(
                f"Comparison with {item.competitor!r} needs all six condition checks"
            )
        all_aligned = all(check.status == "aligned" for check in checks)
        if item.like_for_like and not all_aligned:
            raise ValueError(
                f"Comparison with {item.competitor!r} has unaligned conditions"
            )
        if not item.like_for_like and item.verdict != "insufficient":
            raise ValueError(
                f"Comparison with {item.competitor!r} needs an insufficient verdict"
            )
        if item.verdict == "clear" and not item.like_for_like:
            raise ValueError(
                f"Clear advantage over {item.competitor!r} needs comparable conditions"
            )


def validate_citations(
    report: CompetitorComparison,
    records: list[Evidence],
    company: str,
    competitors: list[str],
) -> list[dict[str, Any]]:
    """Reject unknown and wrong-company IDs; return cited source metadata.

    This validates citation ownership and structural consistency. It cannot prove
    that every generated sentence is semantically entailed by a cited document.
    """
    _validate_requested_structure(report, company, competitors)
    by_id = {record.id: record for record in records}
    cited: dict[str, Evidence] = {}

    def check(ids: list[str], owner: str, label: str, required: bool) -> None:
        for record in validate_evidence_ids(
            ids, by_id, label, required, owner=owner, unique=True
        ):
            cited[record.id] = record

    for index, item in enumerate(report.comparisons):
        if not any(
            record.company.strip().casefold() == item.competitor.strip().casefold()
            for record in records
        ) and any(check.status != "unverified" for check in item.condition_checks):
            raise ValueError(
                f"comparisons[{index}]: absent competitor evidence requires six unverified conditions"
            )
        required = item.verdict != "insufficient"
        check(
            item.target_evidence_ids, company, f"comparisons[{index}].target", required
        )
        check(
            item.competitor_evidence_ids,
            item.competitor,
            f"comparisons[{index}].competitor",
            required,
        )
        if item.like_for_like and (
            not item.target_evidence_ids or not item.competitor_evidence_ids
        ):
            raise ValueError(
                f"comparisons[{index}]: direct comparison needs both companies' evidence"
            )

    for index, risk in enumerate(report.risks):
        check(
            risk.evidence_ids, company, f"risks[{index}]", risk.status != "insufficient"
        )
        if risk.status == "insufficient":
            risk.adoption_impact = "확인 불가: 대상 기업 자료 부족"
            risk.scaling_impact = "확인 불가: 대상 기업 자료 부족"
    check(
        report.defensibility.evidence_ids,
        company,
        "defensibility",
        report.defensibility.status == "supported",
    )
    comparison_ids = {
        evidence_id
        for item in report.comparisons
        for evidence_id in (*item.target_evidence_ids, *item.competitor_evidence_ids)
    }
    risk_ids = {
        evidence_id for risk in report.risks for evidence_id in risk.evidence_ids
    }
    defense_ids = set(report.defensibility.evidence_ids)
    scores = {score.criterion: score for score in report.criterion_scores}
    for criterion in SCORE_CRITERIA:
        score = scores[criterion]
        score_ids = set(score.evidence_ids)
        if criterion in {"defensibility", "adoption_risk"}:
            check(score.evidence_ids, company, f"criterion_scores.{criterion}", False)
        if criterion in {"differentiation", "comparable_performance"}:
            if not score_ids.issubset(comparison_ids):
                raise ValueError(f"{criterion} score must cite a comparison finding")
            for evidence_id in score.evidence_ids:
                if evidence_id not in by_id:
                    raise ValueError(
                        f"{criterion} score cites unknown ID: {evidence_id}"
                    )
                cited[evidence_id] = by_id[evidence_id]
            if len(score.evidence_ids) != len(score_ids):
                raise ValueError(f"{criterion} score repeats an evidence ID")
            qualified = [
                item for item in report.comparisons if item.verdict != "insufficient"
            ]
            if score.rating > 0 and not any(
                score_ids.intersection(item.target_evidence_ids)
                and score_ids.intersection(item.competitor_evidence_ids)
                for item in qualified
            ):
                raise ValueError(
                    f"{criterion} rating needs both companies' comparable evidence"
                )
        elif criterion == "defensibility":
            if not score_ids.issubset(defense_ids):
                raise ValueError(
                    "Defensibility score must cite the defensibility finding"
                )
            if (
                score.rating > 0
                and report.defensibility.status == "insufficient"
            ):
                raise ValueError(
                    "Insufficient defensibility cannot receive a numeric rating"
                )
        else:
            if not score_ids.issubset(risk_ids):
                raise ValueError("Adoption risk score must cite risk findings")
            if score.rating > 0:
                if any(risk.status == "insufficient" for risk in report.risks):
                    raise ValueError(
                        "An unknown risk category prevents an adoption risk rating"
                    )
                if any(
                    not score_ids.intersection(risk.evidence_ids)
                    for risk in report.risks
                ):
                    raise ValueError(
                        "Adoption risk rating needs evidence from all risk categories"
                    )
    return source_metadata(cited.values(), include_company=True)


def _build_scorecard(report: CompetitorComparison) -> dict[str, Any]:
    by_criterion = {score.criterion: score for score in report.criterion_scores}
    criteria = []
    for criterion in SCORE_CRITERIA:
        score = by_criterion[criterion]
        rationale = score.rationale
        if score.rating == 0:
            rationale += " 필수 근거가 부족해 0점 처리했다."
        criteria.append(
            {
                "criterion": criterion,
                "rating": score.rating,
                "points": score.rating * 5,
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
