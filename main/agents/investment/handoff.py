"""투자 판단 결과를 후속 에이전트가 읽을 수 있는 입력으로 묶습니다."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .schemas import InvestmentJudgeState


SOURCE_OUTPUT_KEYS = (
    "technology_summary",
    "technical_score",
    "market_evaluation",
    "competitor_comparison",
    "competitor_score",
)


def build_handoff_payload(
    state: InvestmentJudgeState,
    evaluation: dict[str, Any],
    research_requests: list[dict[str, str]],
    policy: dict[str, Any],
) -> dict[str, Any]:
    """회사·판정·원본 평가 출력·검증 근거를 하나의 독립적인 값으로 반환합니다."""
    company = deepcopy(state["current_candidate"])
    return {
        "company_id": company["id"],
        "company": company,
        "company_data": deepcopy(state.get("company_data") or company.get("base_rag")),
        "evaluation": deepcopy(evaluation),
        "evidence_registry": deepcopy(state["evidence_registry"]),
        "research_requests": deepcopy(research_requests),
        "evaluation_policy": deepcopy(policy),
        "source_outputs": {
            key: deepcopy(state.get(key)) for key in SOURCE_OUTPUT_KEYS
        },
    }
