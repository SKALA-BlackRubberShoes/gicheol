from __future__ import annotations

import unittest

from main.baseRAG import BaseRAG
from LSH.marketEvaluation import (
    CriterionResult,
    MarketEvaluationAgent,
    MarketEvaluationDraft,
    MarketResearchPlan,
    WebSearchResult,
    make_market_node,
)
from LSH.marketRAG import MarketChunk, MarketSearchHit


class FakeMarketRAG:
    """네트워크 없이 에이전트 조립을 검증하는 PDF 검색기입니다."""

    def retrieve(self, query: str, n_results: int = 5):
        return [
            MarketSearchHit(
                chunk=MarketChunk(
                    chunk_id="chunk-1",
                    document_id="IFR_SERVICE_ROBOTS_2026",
                    title="World Robotics 2026 - Service Robots",
                    publisher="International Federation of Robotics",
                    published_year=2026,
                    source_url="https://ifr.org/wr-service-robots",
                    source_file="service.pdf",
                    page=4,
                    chunk_index=0,
                    region="GLOBAL",
                    language="en",
                    market_segments=["professional_service_robot"],
                    text="Professional service robot demand continues to grow.",
                ),
                score=0.8,
            )
        ]


class FakeWebSearch:
    def search(self, queries: list[str], *, max_results_per_query: int = 5):
        return [
            WebSearchResult(
                query=queries[0],
                title="Customer adoption example",
                url="https://example.com/adoption",
                publisher="example.com",
                published_at="2026-01-01",
                content="A customer introduced the product in a paid operation.",
            )
        ]


class FakeEvaluationBackend:
    def create_research_plan(self, company):
        return MarketResearchPlan(
            market_definition=(
                f"이 기업은 한국의 제조기업에게 인력 부족 문제를 해결하는 "
                f"{company.raw['대표제품']}을 판매한다."
            ),
            pdf_queries=["시장 규모", "성장률", "도입 장벽"],
            web_queries=["고객 계약", "제품 도입", "제품 가격"],
        )

    def evaluate(self, *, company, plan, evidence):
        scores = [4, 3, 4, 3, 4]
        names = [
            "customer_willingness",
            "market_size",
            "growth_timing",
            "adoption_feasibility",
            "scalability",
        ]
        return MarketEvaluationDraft(
            criteria=[
                CriterionResult(
                    criterion=name,
                    score=score,
                    reason=f"{name} test reason",
                    source_ids=["P001", "W001"],
                )
                for name, score in zip(names, scores)
            ],
            market_risks=["초기 시장 검증 필요"],
        )


class MarketEvaluationAgentTest(unittest.TestCase):
    def setUp(self):
        self.base_rag = BaseRAG()
        self.agent = MarketEvaluationAgent(
            base_rag=self.base_rag,
            market_rag=FakeMarketRAG(),
            web_search=FakeWebSearch(),
            evaluation_backend=FakeEvaluationBackend(),
        )

    def tearDown(self):
        self.base_rag.close()

    def test_agent_returns_weighted_result_and_sources(self):
        result = self.agent.evaluate("1")

        self.assertEqual(result.company_id, "1")
        self.assertEqual(len(result.criteria), 5)
        self.assertEqual(result.market_score_100, 71.0)
        self.assertEqual(result.investment_score_25, 17.75)
        self.assertEqual({source.source_id for source in result.evidence}, {"P001", "W001"})

    def test_langgraph_node_returns_only_market_evaluation_update(self):
        node = make_market_node(self.agent)
        update = node({"company_id": "1"})

        self.assertEqual(set(update), {"market_evaluation"})
        self.assertEqual(update["market_evaluation"]["company_id"], "1")


if __name__ == "__main__":
    unittest.main()
