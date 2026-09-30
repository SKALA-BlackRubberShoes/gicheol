"""터미널 프롬프트로 회사 선택부터 투자 판단·PDF 생성까지 전체 그래프를 실행합니다."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

from langgraph.types import Command

from main.agents.common.llm import resolve_chat_model
from main.agents.competitor_selection import CompetitorSelectionAgent
from main.agents.market import (
    MarketEvaluationAgent,
    OpenAIMarketEvaluationBackend,
    OpenAIWebSearch,
)
from main.agents.start import StartAgent
from main.graph import (
    make_comparison_node,
    make_competitor_selection_node,
    make_investment_node,
    make_market_node,
    make_report_node,
    make_start_node,
    make_technology_node,
)
from main.graph.workflow import build_graph
from main.rag.company import BaseRAG
from main.rag.market import MarketRAG


def _print_feedback(state: dict) -> None:
    if state.get("message"):
        print(state["message"])
    decision = state.get("decision")
    if decision:
        label = "투자 검토 추천" if decision == "invest" else "투자 판단 보류"
        print(f"\n{label} · 총점: {state.get('total_score')}")
        for reason in state.get("decision_reasons", []):
            print(f"  - {reason}")


def _read_prompt(payload: dict) -> str | None:
    _print_feedback(payload)
    while True:
        prompt = input("\n기업 선택 조건을 입력하세요 (종료: quit): ").strip()
        if prompt.casefold() in {"quit", "exit", "q", "종료"}:
            return None
        if prompt:
            return prompt
        print("프롬프트를 입력해주세요.")


def _company_label(rag: BaseRAG, company_id: str) -> str:
    company = rag.get_company(company_id)
    return f"{company.company_name} ({company_id})" if company else company_id


def _write_json(output: Path, state: dict) -> None:
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    try:
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"결과 저장: {output}")


def run_terminal(graph, rag: BaseRAG, *, output: Path | None = None) -> dict | None:
    """interrupt에서 입력을 받아 같은 그래프 실행을 재개합니다."""
    config = {"configurable": {"thread_id": str(uuid4())}}
    command: dict | Command = {}
    while True:
        payload = None
        for update in graph.stream(command, config, stream_mode="updates"):
            if "__interrupt__" in update:
                payload = update["__interrupt__"][0].value
                continue
            for node, result in update.items():
                if node == "start" and result.get("company_id"):
                    print(f"선택 회사: {_company_label(rag, result['company_id'])}")
                elif node == "select_competitors" and result.get("competitor_ids"):
                    if result.get("message"):
                        print(result["message"])
                    names = [
                        _company_label(rag, item) for item in result["competitor_ids"]
                    ]
                    print(f"선정 비교 기업: {', '.join(names)}")
                    print("시장·경쟁·기술 평가를 병렬 실행합니다.")
                elif node in {"market", "competition", "technology"}:
                    labels = {
                        "market": "시장",
                        "competition": "경쟁",
                        "technology": "기술",
                    }
                    print(f"{labels[node]} 평가 완료")
                elif node == "investment":
                    print("투자 판단 완료")
                    if result.get("decision") == "invest":
                        print("PDF 보고서를 생성합니다.")
                elif node == "report":
                    print(f"PDF 보고서 생성 완료 · {result.get('report_page_count')}쪽")
                    for key, label in (
                        ("report_pdf_path", "PDF"),
                        ("report_markdown_path", "Markdown"),
                        ("report_json_path", "검증 기록"),
                    ):
                        if result.get(key):
                            print(f"{label}: {result[key]}")
                    for warning in result.get("report_warnings", []):
                        print(f"보고서 경고: {warning}")

        if payload is None:
            state = dict(graph.get_state(config).values)
            _print_feedback(state)
            if output is not None:
                _write_json(output, state)
            return state

        prompt = _read_prompt(payload)
        if prompt is None:
            print("실행을 종료합니다.")
            return None
        print("회사와 경쟁사를 선택합니다.")
        command = Command(resume=prompt)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="스타트업 투자 분석 전체 LangGraph 실행"
    )
    parser.add_argument(
        "--model",
        default="openai:gpt-4.1",
        help="회사·경쟁사 선택과 기술·경쟁 평가 모델",
    )
    parser.add_argument("--start-model", default=os.getenv("START_AGENT_MODEL") or None)
    parser.add_argument(
        "--team-model", help="투자 판단의 팀 평가 모델; 생략 시 --model 공유"
    )
    parser.add_argument(
        "--recommend-min-score", type=float, help="투자 추천 최소 총점; 기본 80"
    )
    parser.add_argument(
        "--output", type=Path, help="추천으로 종료할 때 전체 State를 JSON으로 저장"
    )
    args = parser.parse_args()
    if (
        args.recommend_min_score is not None
        and not 0 <= args.recommend_min_score <= 100
    ):
        parser.error("--recommend-min-score는 0~100이어야 합니다.")

    try:
        # 생성 도중 실패하거나 사용자가 종료해도 이미 만든 자원을 닫습니다.
        with ExitStack() as resources:
            base_rag = BaseRAG()
            resources.callback(base_rag.close)
            market_rag = MarketRAG()
            resources.callback(market_rag.close)
            web_search = OpenAIWebSearch()
            resources.callback(web_search.close)
            evaluator = OpenAIMarketEvaluationBackend()
            resources.callback(evaluator.close)

            llm = resolve_chat_model(args.model)
            start_llm = (
                resolve_chat_model(args.start_model) if args.start_model else llm
            )
            team_llm = resolve_chat_model(args.team_model) if args.team_model else llm
            start_agent = StartAgent(base_rag, start_llm)
            competitor_agent = CompetitorSelectionAgent(base_rag, model=llm)
            market_agent = MarketEvaluationAgent(
                base_rag=base_rag,
                market_rag=market_rag,
                web_search=web_search,
                evaluation_backend=evaluator,
            )
            judge = make_investment_node(base_rag, model=team_llm)
            market = make_market_node(market_agent)

            def market_node(state):
                # 유효한 회사·경쟁사가 선정된 경우에만 PDF 색인을 준비합니다.
                market_rag.build_index()
                return market(state)

            def investment_node(state):
                if args.recommend_min_score is None:
                    return judge(state)
                # 시작 노드가 State를 초기화하므로 판단 직전에 실행 옵션을 적용합니다.
                policy = {"recommend_min_score": args.recommend_min_score}
                result = judge({**state, "decision_policy": policy})
                return {**result, "decision_policy": policy}

            graph = build_graph(
                start_node=make_start_node(start_agent),
                competitor_node=make_competitor_selection_node(competitor_agent),
                market_node=market_node,
                competition_node=make_comparison_node(base_rag, model=llm),
                technology_node=make_technology_node(base_rag, model=llm),
                investment_node=investment_node,
                report_node=make_report_node(),
            )

            print("기업 RAG 색인을 준비합니다. 기존 컬렉션이 있으면 재사용합니다.")
            print(f"기업 색인: {base_rag.build_index()}개")
            run_terminal(graph, base_rag, output=args.output)
        return 0
    except (KeyboardInterrupt, EOFError):
        print("\n실행을 종료합니다.")
        return 0
    except Exception as exc:
        # API·서버 오류를 투자 보류로 바꾸지 않고 실행 오류로 알립니다.
        print(f"실행 실패: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
