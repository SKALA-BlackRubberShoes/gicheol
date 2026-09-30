"""시장성 핵심 평가표의 가중점수를 계산합니다.

LLM은 5개 항목의 1~5점만 판단합니다. 산술 계산은 이 모듈에서 수행해야
기업 30개에 완전히 동일한 계산식을 적용할 수 있습니다.
"""

from __future__ import annotations

from collections.abc import Iterable

from .schemas import CriterionName, CriterionResult


CRITERION_ORDER: tuple[CriterionName, ...] = (
    "customer_willingness",
    "market_size",
    "growth_timing",
    "adoption_feasibility",
    "scalability",
)

WEIGHTS: dict[CriterionName, int] = {
    "customer_willingness": 25,
    "market_size": 25,
    "growth_timing": 20,
    "adoption_feasibility": 20,
    "scalability": 10,
}

CRITERION_LABELS: dict[CriterionName, str] = {
    "customer_willingness": "고객 문제와 지불의향",
    "market_size": "시장 크기와 획득 가능성",
    "growth_timing": "성장성과 진입 타이밍",
    "adoption_feasibility": "고객 도입 가능성",
    "scalability": "사업 확장성",
}


def scores_from_results(results: Iterable[CriterionResult]) -> dict[CriterionName, int]:
    """평가 결과 5개를 중복 없는 ``항목: 점수`` 딕셔너리로 바꿉니다."""

    scores: dict[CriterionName, int] = {}
    for result in results:
        if result.criterion in scores:
            raise ValueError(f"Duplicate criterion: {result.criterion}")
        scores[result.criterion] = result.score

    missing = [name for name in CRITERION_ORDER if name not in scores]
    extra = [name for name in scores if name not in WEIGHTS]
    if missing or extra:
        raise ValueError(f"Exactly five market criteria are required; missing={missing}, extra={extra}")
    return scores


def calculate_market_score(scores: dict[CriterionName, int]) -> tuple[float, float]:
    """1~5점 평가를 시장성 /100점과 투자평가 반영 /25점으로 변환합니다."""

    missing = [name for name in CRITERION_ORDER if name not in scores]
    extra = [name for name in scores if name not in WEIGHTS]
    if missing or extra:
        raise ValueError(f"Exactly five market criteria are required; missing={missing}, extra={extra}")
    for name, score in scores.items():
        if type(score) is not int or not 1 <= score <= 5:
            raise ValueError(f"{name} score must be an integer from 1 to 5")

    score_100 = sum(WEIGHTS[name] * scores[name] / 5 for name in CRITERION_ORDER)
    score_25 = score_100 * 0.25
    return round(score_100, 2), round(score_25, 2)

