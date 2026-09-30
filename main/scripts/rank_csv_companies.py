"""CSV 전 회사의 실제 평가를 누적하고 총점 1위 보고서만 생성한다."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

from main.agents.market import MarketEvaluationAgent, OpenAIMarketEvaluationBackend, OpenAIWebSearch
from main.agents.investment import TeamWebResearcher
from main.agents.report import generate_report
from main.graph.batch import run_csv_ranking
from main.graph.nodes import (
    make_comparison_node, make_investment_node, make_market_node, make_technology_node,
)
from main.paths import DEFAULT_CSV_PATH
from main.rag.company import BaseRAG
from main.rag.market import MarketRAG


def main() -> int:
    parser = argparse.ArgumentParser(description="CSV 전체 회사를 평가해 총점 1위 보고서 생성")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV_PATH)
    parser.add_argument("--output-dir", type=Path, default=Path("outputs/rankings"))
    parser.add_argument("--env-file", type=Path, help="API 키를 읽을 .env 파일")
    parser.add_argument("--model", default="openai:gpt-4.1", help="기술·경쟁 평가 모델")
    parser.add_argument("--team-model", default="openai:gpt-4.1", help="팀 10점 평가 모델")
    parser.add_argument("--competitors", type=int, default=2, help="회사별 비교할 경쟁사 수")
    parser.add_argument("--rebuild-market-index", action="store_true")
    args = parser.parse_args()
    if args.competitors < 1:
        parser.error("--competitors는 1 이상이어야 합니다.")
    if args.env_file:
        if not args.env_file.is_file():
            parser.error("--env-file 파일이 없습니다.")
        load_dotenv(args.env_file, override=False)

    run_dir = args.output_dir.expanduser().resolve() / uuid4().hex[:12]
    with ExitStack() as stack:
        company_rag = BaseRAG(args.csv)
        stack.callback(company_rag.close)
        market_rag = MarketRAG()
        stack.callback(market_rag.close)
        web_search = OpenAIWebSearch()
        stack.callback(web_search.close)
        team_web_search = OpenAIWebSearch(purpose="team")
        stack.callback(team_web_search.close)
        market_backend = OpenAIMarketEvaluationBackend()
        stack.callback(market_backend.close)
        market_rag.build_index(rebuild=args.rebuild_market_index)

        market_agent = MarketEvaluationAgent(
            base_rag=company_rag, market_rag=market_rag,
            web_search=web_search, evaluation_backend=market_backend,
        )
        technology_node = make_technology_node(company_rag, model=args.model)
        comparison_node = make_comparison_node(company_rag, model=args.model)
        market_node = make_market_node(market_agent)
        investment_node = make_investment_node(
            company_rag, model=args.team_model,
            team_researcher=TeamWebResearcher(team_web_search),
        )

        def evaluate(company_id: str, peers: list[str]) -> dict:
            state = {"company_id": company_id, "competitor_ids": peers}
            for node in (technology_node, comparison_node, market_node, investment_node):
                state.update(node(state))
            return state

        result = run_csv_ranking(
            company_rag.list_companies(), evaluate, generate_report, run_dir,
            competitors_per_company=args.competitors,
        )

    print(f"평가 시도: {result['attempted_count']}/{result['company_count']} "
          f"(성공 {result['evaluated_count']}, 실패 {result['failed_count']})")
    print(f"1위: {result['ranking'][0]['company_name']} ({result['winner_company_id']}), "
          f"{result['ranking'][0]['score']:g}점")
    print(f"전체 결과: {run_dir / 'ranking.json'}")
    print(f"보고서 PDF: {result['report']['report_pdf_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
