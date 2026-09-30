"""State의 회사 ID를 에이전트 입력과 결과에 연결하는 얇은 노드입니다."""

from __future__ import annotations

from typing import Any, Literal
from langchain_core.runnables import RunnableConfig
from main.rag.company import BaseRAG
from main.agents.start import StartAgent
from main.agents.technology import run_agent as run_technology
from main.agents.competition import run_agent as run_comparison
from main.agents.competitor_selection import CompetitorSelectionAgent
from main.agents.market import MarketEvaluationAgent
from main.agents.investment import (
    make_rag_investment_judge_node,
    route_after_investment as route_after_investment,
)
from main.agents.investment.agent import TeamResearcher
from main.agents.common.evidence import as_evidence, get_selected_company
from main.agents.common.llm import resolve_chat_model
from main.agents.report import make_report_node as _make_report_node
from .state import InvestmentState, reset_judge_state


def make_start_node(agent: StartAgent):
    """회사 재선택 시 이전 평가 결과를 지웁니다. 입력 State는 수정하지 않습니다."""

    def start_node(
        state: InvestmentState, config: RunnableConfig | None = None
    ) -> dict:
        result = agent.invoke(state, config=config)
        result.update(
            technology_summary=None,
            technical_score=None,
            competitor_comparison=None,
            competitor_score=None,
            market_evaluation=None,
            **reset_judge_state(),
        )
        # 다른 회사를 선택하면 이전 회사용으로 지정한 경쟁사도 다시 받아야 합니다.
        if (
            state.get("company_id") is not None
            and state["company_id"] != result["company_id"]
        ):
            result["competitor_ids"] = []
        return result

    return start_node


def make_competitor_selection_node(agent: CompetitorSelectionAgent):
    """회사 ID를 선정 에이전트에 전달하고 State 갱신값만 반환합니다."""

    def selection_node(
        state: InvestmentState, config: RunnableConfig | None = None
    ) -> dict:
        return agent.select(state.get("company_id"), config=config)

    return selection_node


def make_technology_node(rag: BaseRAG, model: str | Any = "openai:gpt-4.1"):
    """선택된 회사의 기술 요약과 점수 갱신값만 반환합니다."""
    llm = resolve_chat_model(model)

    def technology_node(state: InvestmentState) -> dict:
        record = get_selected_company(rag, state.get("company_id"))
        evidence = [as_evidence(record)]
        result = run_technology(
            {"company": record.company_name},
            rag,
            model=llm,
            search_company=lambda _name: evidence,
        )
        return {
            "technology_summary": result["technology_summary"],
            "technical_score": result["technical_score"],
        }

    return technology_node


def make_comparison_node(rag: BaseRAG, model: str | Any = "openai:gpt-4.1"):
    """호출자가 competitor_ids에 지정한 회사들만 비교합니다."""
    llm = resolve_chat_model(model)

    def comparison_node(state: InvestmentState) -> dict:
        target = get_selected_company(rag, state.get("company_id"))
        ids = state.get("competitor_ids")
        if not isinstance(ids, list) or not ids:
            raise ValueError("state.competitor_ids must be a nonempty list of IDs")
        records = [target, *(get_selected_company(rag, item) for item in ids)]
        names = [record.company_name for record in records]
        if len({name.casefold() for name in names}) != len(names):
            raise ValueError(
                "Target and competitor IDs must identify distinct companies"
            )
        evidence = {record.company_name: [as_evidence(record)] for record in records}
        result = run_comparison(
            {"company": target.company_name, "competitors": names[1:]},
            rag,
            model=llm,
            search_company=lambda name: evidence[name],
        )
        return {
            "competitor_comparison": result["competitor_comparison"],
            "competitor_score": result["competitor_score"],
        }

    return comparison_node


def make_market_node(agent: MarketEvaluationAgent):
    """선택한 company_id를 조사하고 시장 평가 갱신값만 반환합니다."""

    def market_node(state: InvestmentState) -> dict:
        company_id = state.get("company_id")
        if not isinstance(company_id, str) or not company_id.strip():
            raise ValueError("market_node requires a nonempty state['company_id']")
        return {"market_evaluation": agent.evaluate(company_id.strip()).model_dump()}

    return market_node


def make_investment_node(
    rag: BaseRAG,
    model: Any = None,
    *,
    team_researcher: TeamResearcher | None = None,
    research_mode: Literal["if_missing", "always", "off"] = "if_missing",
):
    """세 영역의 평가 결과와 검증된 추가 근거를 투자 판단에 연결합니다."""
    return make_rag_investment_judge_node(
        rag, model, team_researcher=team_researcher, research_mode=research_mode,
    )


def make_report_node(**options):
    """추천·보류 payload를 받아 최종 보고서 갱신값만 반환합니다."""
    return _make_report_node(**options)
