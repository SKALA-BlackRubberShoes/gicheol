"""사용자 입력 → 회사·경쟁사 선택 → 병렬 평가 → 투자 판단 → PDF 생성 그래프입니다."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.runnables import RunnableLambda
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .nodes import route_after_investment
from .state import InvestmentState, new_request_state


def _prompt_node(state: InvestmentState) -> dict:
    """터미널 실행 파일에서 받은 새 프롬프트로 평가 State를 초기화합니다."""
    prompt = interrupt(
        {
            "kind": "prompt",
            "message": state.get("message"),
            "decision": state.get("decision"),
            "total_score": state.get("total_score"),
            "decision_reasons": state.get("decision_reasons", []),
        }
    )
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("사용자 프롬프트는 비어 있지 않은 문자열이어야 합니다.")
    return new_request_state(prompt.strip())


def _after_start(state: InvestmentState) -> str:
    return "select_competitors" if state.get("company_id") else "prompt"


def build_graph(
    *,
    start_node: Callable[..., dict],
    competitor_node: Callable[..., dict],
    market_node: Callable[..., dict],
    competition_node: Callable[..., dict],
    technology_node: Callable[..., dict],
    investment_node: Callable[..., dict],
    report_node: Callable[..., dict],
    checkpointer: Any = None,
):
    """기존 노드 함수를 주입받습니다. 각 평가의 반환 dict를 별도 State 키에 저장합니다."""
    graph = StateGraph(InvestmentState)
    nodes = {
        "prompt": _prompt_node,
        "start": start_node,
        "select_competitors": competitor_node,
        "market": market_node,
        "competition": competition_node,
        "technology": technology_node,
        "investment": investment_node,
        "report": report_node,
    }
    # 기존 함수의 config 인자를 LangChain이 전달하도록 연결합니다.
    for name, node in nodes.items():
        graph.add_node(name, RunnableLambda(node))

    graph.add_edge(START, "prompt")
    graph.add_edge("prompt", "start")
    graph.add_conditional_edges(
        "start",
        _after_start,
        {"select_competitors": "select_competitors", "prompt": "prompt"},
    )
    for name in ("market", "competition", "technology"):
        graph.add_edge("select_competitors", name)
    # 세 평가를 병렬 실행하고 모두 완료한 뒤 투자 판단을 한 번 호출합니다.
    graph.add_edge(["market", "competition", "technology"], "investment")
    graph.add_conditional_edges(
        "investment",
        route_after_investment,
        {"report": "report", "hold": "prompt"},
    )
    graph.add_edge("report", END)
    return graph.compile(
        checkpointer=checkpointer if checkpointer is not None else InMemorySaver()
    )
