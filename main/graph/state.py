"""각 노드가 공유하는 State 키입니다. 값은 보고서별로 덮어씁니다."""

from typing import TypedDict


class InvestmentState(TypedDict, total=False):
    prompt: str
    company_id: str | None
    message: str | None
    competitor_ids: list[str]
    technology_summary: dict | None
    technical_score: dict | None
    competitor_comparison: dict | None
    competitor_score: dict | None
    market_evaluation: dict | None


def new_request_state(
    prompt: str, *, competitor_ids: list[str] | None = None
) -> InvestmentState:
    """새 사용자 요청을 시작할 때 이전 회사의 평가가 섞이지 않는 State를 만듭니다."""
    return InvestmentState(
        prompt=prompt,
        company_id=None,
        message=None,
        competitor_ids=list(competitor_ids or []),
        technology_summary=None,
        technical_score=None,
        competitor_comparison=None,
        competitor_score=None,
        market_evaluation=None,
    )
