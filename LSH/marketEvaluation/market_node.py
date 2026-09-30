"""MarketEvaluationAgent를 LangGraph 노드 형태로 바꾸는 얇은 어댑터입니다."""

from __future__ import annotations

from typing import TypedDict

from .market_agent import MarketEvaluationAgent


class MarketNodeState(TypedDict, total=False):
    """시장성 노드가 읽고 쓰는 최소 State 계약입니다.

    팀의 전체 Graph State에 더 많은 키가 있어도 괜찮습니다. 이 노드는
    ``company_id``만 읽고 ``market_evaluation``만 반환합니다.
    """

    company_id: str
    market_evaluation: dict


def make_market_node(agent: MarketEvaluationAgent):
    """주입받은 에이전트를 사용하는 LangGraph 호환 노드를 반환합니다."""

    def market_node(state: MarketNodeState) -> MarketNodeState:
        company_id = state.get("company_id")
        if not isinstance(company_id, str) or not company_id.strip():
            raise ValueError("market_node requires a nonempty state['company_id']")

        result = agent.evaluate(company_id.strip())
        return {"market_evaluation": result.model_dump()}

    return market_node

