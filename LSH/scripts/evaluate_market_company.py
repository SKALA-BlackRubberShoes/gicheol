"""회사 ID 하나로 시장성 평가 전체 흐름을 실행하는 예제입니다."""

from __future__ import annotations

import argparse

from main.baseRAG import BaseRAG
from LSH.marketEvaluation import (
    MarketEvaluationAgent,
    OpenAIMarketEvaluationBackend,
    OpenAIWebSearch,
)
from LSH.marketRAG import MarketRAG


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate one company's market attractiveness")
    parser.add_argument("company_id", help="company_id from the 30-company CSV")
    args = parser.parse_args()

    base_rag = BaseRAG()
    market_rag = MarketRAG()
    web_search = OpenAIWebSearch()
    evaluator = OpenAIMarketEvaluationBackend()

    try:
        # 기존 컬렉션이 있으면 임베딩 없이 재사용합니다.
        market_rag.build_index()
        agent = MarketEvaluationAgent(
            base_rag=base_rag,
            market_rag=market_rag,
            web_search=web_search,
            evaluation_backend=evaluator,
        )
        result = agent.evaluate(args.company_id)
        print(result.model_dump_json(indent=2))
    finally:
        evaluator.close()
        web_search.close()
        market_rag.close()
        base_rag.close()


if __name__ == "__main__":
    main()
