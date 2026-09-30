"""공통 State와 BaseRAG 조회 결과를 투자 판단 입력에 연결합니다.

기존 분석 노드의 점수와 인용 ID는 그대로 전달합니다. 이 노드의 출력만으로
근거의 원문·출처 유형이나 적격성을 검증할 수 없으므로 이를 생성하지 않습니다.
"""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING, Any, Callable, Literal

from .agent import InvestmentJudgeState, TeamResearcher, make_investment_judge_node

if TYPE_CHECKING:
    from main.rag.company import BaseRAG


def _unique_ids(items: list[dict[str, Any]], field: str) -> list[str]:
    return list(dict.fromkeys(
        ref for item in items if isinstance(item, dict)
        for ref in item.get(field, []) if isinstance(ref, str) and ref.strip()
    ))


def _reason(items: list[dict[str, Any]], field: str) -> str:
    text = [item[field].strip() for item in items
            if isinstance(item, dict) and isinstance(item.get(field), str) and item[field].strip()]
    return "; ".join(text) or "담당 에이전트의 세부 평가 사유 확인 필요"


def _adapt_existing_analyses(working: dict[str, Any]) -> None:
    """main의 평가 결과를 판단 에이전트의 100점 입력 형식으로 옮깁니다.

    명시적으로 제공한 *_analysis는 우선합니다. 축약된 sources 메타데이터는
    Evidence 계약의 원문·출처 유형을 증명하지 못해 registry에 넣지 않습니다.
    """
    company_id = working["current_candidate"]["id"]
    company_name = working["current_candidate"]["name"]
    for field, identity, expected in (
        ("technology_summary", "company", company_name),
        ("competitor_comparison", "company", company_name),
        ("market_evaluation", "company_id", company_id),
        ("market_evaluation", "company_name", company_name),
    ):
        result = working.get(field)
        if isinstance(result, dict) and identity in result and result[identity] != expected:
            raise ValueError(f"{field}.{identity}와 선택된 기업이 다릅니다.")

    technical = working.get("technical_score")
    summary = working.get("technology_summary")
    if working.get("technology_analysis") is None and isinstance(technical, dict):
        criteria = technical.get("criteria") or []
        working["technology_analysis"] = {
            "summary": (summary or {}).get("technology_summary", "기술 분석 결과")
            if isinstance(summary, dict) else "기술 분석 결과",
            "score": technical.get("total"), "max_score": technical.get("max"),
            "reason": _reason(criteria, "rationale"),
            "evidence_ids": _unique_ids(criteria, "evidence_ids"),
            "missing_items": (
                (["제품·기술력: 근거 부족 항목 0점 처리"]
                 if technical.get("status") == "insufficient_evidence" else [])
                + (list((summary or {}).get("key_unknowns") or [])
                   if isinstance(summary, dict) else [])
            ),
            "conflicts": [],
            "breakdown": deepcopy(summary),
        }

    competitive = working.get("competitor_score")
    comparison = working.get("competitor_comparison")
    if working.get("competition_analysis") is None and isinstance(competitive, dict):
        criteria = competitive.get("criteria") or []
        working["competition_analysis"] = {
            "summary": "경쟁 비교 결과",
            "score": competitive.get("total"), "max_score": competitive.get("max"),
            "reason": _reason(criteria, "rationale"),
            "evidence_ids": _unique_ids(criteria, "evidence_ids"),
            "missing_items": (
                (["경쟁 우위: 근거 부족 항목 0점 처리"]
                 if competitive.get("status") == "insufficient_evidence" else [])
                + (list((comparison or {}).get("key_unknowns") or [])
                   if isinstance(comparison, dict) else [])
            ),
            "conflicts": [],
            "breakdown": deepcopy(comparison),
        }

    market = working.get("market_evaluation")
    if working.get("market_analysis") is None and isinstance(market, dict):
        criteria = market.get("criteria") or []
        working["market_analysis"] = {
            "summary": "시장성 평가 결과",
            "score": market.get("market_score_100"), "max_score": 100,
            "reason": _reason(criteria, "reason"),
            "evidence_ids": _unique_ids(criteria, "source_ids"),
            "market_definition": market.get("market_definition"),
            "missing_items": [], "conflicts": [],
            "breakdown": deepcopy(market),
        }


def prepare_judge_state(rag: BaseRAG, state: dict[str, Any]) -> InvestmentJudgeState:
    """기업 ID를 정확 조회하고 기존 평가·검증 근거를 보존합니다."""
    company_id = state.get("company_id")
    if not isinstance(company_id, str) or not company_id.strip():
        raise ValueError("company_id는 비어 있지 않은 문자열이어야 합니다.")
    company_id = company_id.strip()
    candidate = state.get("current_candidate") or {}
    if not isinstance(candidate, dict):
        raise ValueError("current_candidate는 dict여야 합니다.")
    if "id" in candidate and candidate["id"] != company_id:
        raise ValueError("company_id와 current_candidate.id가 다릅니다. 기업별 State를 분리하세요.")
    company = rag.get_company(company_id)
    if company is None:
        raise ValueError(f"BaseRAG에 없는 company_id입니다: {company_id}")
    if "name" in candidate and candidate["name"] != company.company_name:
        raise ValueError("current_candidate.name과 BaseRAG 기업명이 다릅니다.")
    working = deepcopy(state)
    working["company_id"] = company_id
    working["company_data"] = company.model_dump()
    working["current_candidate"] = {
        **deepcopy(candidate), "id": company_id, "name": company.company_name,
        "base_rag": deepcopy(working["company_data"]),
    }
    _adapt_existing_analyses(working)
    return working


def make_rag_investment_judge_node(
    rag: BaseRAG,
    llm: Any = None,
    *,
    team_researcher: TeamResearcher | None = None,
    research_mode: Literal["if_missing", "always", "off"] = "if_missing",
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """공유 BaseRAG와 선택적 팀 평가 모델을 사용하는 LangGraph 노드입니다."""
    judge = make_investment_judge_node(
        llm, team_researcher=team_researcher, research_mode=research_mode,
    )

    def node(state: dict[str, Any]) -> dict[str, Any]:
        working = prepare_judge_state(rag, state)
        result = judge(working)
        return {**result, "company_id": working["company_id"],
                "company_data": working["company_data"]}

    return node
