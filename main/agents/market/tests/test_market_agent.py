"""탐색된 기업 ID가 시장성 평가와 점수 계산까지 전달되는지 검증합니다.

외부 OpenAI API와 Qdrant를 호출하지 않고, 각 의존성을 작은 가짜 객체로
대체합니다. 따라서 실패하면 네트워크가 아니라 시장성 평가 비즈니스 로직의
회귀로 판단할 수 있습니다.
"""

from __future__ import annotations

import unittest

from main.agents.market import (
    CRITERION_ORDER,
    CriterionResult,
    MarketEvaluationAgent,
    MarketEvaluationDraft,
    MarketEvaluationError,
    MarketResearchPlan,
    OpenAIWebSearch,
    WebSearchError,
    WebSearchResult,
)
from main.graph import make_market_node
from main.rag.company import CompanyRecord, SourceRef
from main.rag.market import MarketChunk, MarketSearchHit


def _company() -> CompanyRecord:
    """탐색 에이전트가 선택했다고 가정하는 한 기업입니다."""

    return CompanyRecord(
        company_id="17",
        company_name="테스트 로보틱스",
        raw={"대표제품": "AI 로봇", "서비스": "제조 자동화"},
        values={
            "location": "대한민국",
            "sector": "로봇",
            "subsector": "산업용 로봇",
            "technology": "Physical AI",
            "product_type": "로봇",
        },
        content="테스트 로보틱스는 제조 자동화를 위한 AI 로봇을 개발한다.",
        source=SourceRef(csv_path="companies.csv", record_number=18),
    )


class FakeBaseRAG:
    """company_id 정확 조회 여부를 기록합니다."""

    def __init__(self) -> None:
        self.requested_ids: list[str] = []
        self.company = _company()

    def get_company(self, company_id: str) -> CompanyRecord | None:
        self.requested_ids.append(company_id)
        return self.company if company_id == self.company.company_id else None


class FakeMarketRAG:
    """모든 PDF 검색어에 같은 청크를 반환해 중복 제거도 검증합니다."""

    def __init__(self, *, return_evidence: bool = True) -> None:
        self.return_evidence = return_evidence
        self.queries: list[tuple[str, int]] = []
        self.hit = MarketSearchHit(
            chunk=MarketChunk(
                chunk_id="market-doc:page-1:chunk-0",
                document_id="market-doc",
                title="로봇 시장 보고서",
                publisher="테스트 기관",
                published_year=2025,
                source_url="https://example.com/market-report",
                source_file="market-report.pdf",
                page=1,
                chunk_index=0,
                region="Global",
                language="ko",
                market_segments=["robotics"],
                text="로봇 시장 규모와 성장률에 관한 PDF 원문 근거",
            ),
            score=0.91,
        )

    def retrieve(self, query: str, n_results: int = 5) -> list[MarketSearchHit]:
        self.queries.append((query, n_results))
        return [self.hit] if self.return_evidence else []


class FakeWebSearch:
    """동일 URL을 두 번 반환해 URL 기준 중복 제거를 검증합니다."""

    def __init__(self, *, return_evidence: bool = True) -> None:
        self.return_evidence = return_evidence
        self.received_queries: list[list[str]] = []

    def search(self, queries: list[str]) -> list[WebSearchResult]:
        self.received_queries.append(list(queries))
        if not self.return_evidence:
            return []
        item = WebSearchResult(
            query=" | ".join(queries),
            title="고객 도입 사례",
            url="https://example.com/customer-case",
            publisher="example.com",
            published_at="2025-01-01",
            content="유료 도입과 투자 회수기간에 관한 웹 근거",
            content_kind="generated_summary",
        )
        return [item, item.model_copy(deep=True)]


class FailingWebSearch:
    """실제 API timeout처럼 웹 검색 전체 실패를 재현합니다."""

    def search(self, queries: list[str]) -> list[WebSearchResult]:
        raise WebSearchError("simulated timeout")


class FakeBatchingWebSearch(OpenAIWebSearch):
    """API 호출 없이 검색어 분할과 부분 실패 복구만 검증합니다."""

    def __init__(self, *, failed_first_queries: set[str] | None = None) -> None:
        super().__init__(
            timeout_seconds=1,
            max_retries=0,
            max_queries_per_request=2,
        )
        self.failed_first_queries = failed_first_queries or set()
        self.batches: list[list[str]] = []

    def _search_batch(
        self,
        queries: list[str],
        *,
        max_results_per_query: int,
    ) -> list[WebSearchResult]:
        self.batches.append(list(queries))
        if queries[0] in self.failed_first_queries:
            raise WebSearchError("simulated batch timeout")
        return [
            WebSearchResult(
                query=" | ".join(queries),
                title=f"{queries[0]} 근거",
                url=f"https://example.com/{queries[0]}",
                publisher="example.com",
                content=f"{queries[0]} 검색 결과",
                content_kind="generated_summary",
            )
        ]


class FakeEvaluationBackend:
    """기업과 근거를 기록하고 미리 정한 5개 점수를 반환합니다."""

    def __init__(
        self,
        scores: tuple[int, int, int, int, int] = (5, 4, 3, 2, 1),
        *,
        unknown_source_id: bool = False,
    ) -> None:
        self.scores = scores
        self.unknown_source_id = unknown_source_id
        self.planned_company: CompanyRecord | None = None
        self.evaluated_company: CompanyRecord | None = None
        self.received_evidence = []

    def create_research_plan(self, company: CompanyRecord) -> MarketResearchPlan:
        self.planned_company = company
        return MarketResearchPlan(
            market_definition=(
                "이 기업은 국내 제조 고객에게 반복 작업을 자동화하는 AI 로봇을 판매한다."
            ),
            pdf_queries=["로봇 시장 규모", "로봇 시장 성장률", "로봇 도입 장벽"],
            web_queries=["고객 계약", "제품 가격 ROI", "고객 도입 사례"],
        )

    def evaluate(self, *, company, plan, evidence) -> MarketEvaluationDraft:
        self.evaluated_company = company
        self.received_evidence = list(evidence)
        source_ids = [item.source_id for item in evidence]
        if self.unknown_source_id:
            source_ids.append("W999")
        return MarketEvaluationDraft(
            criteria=[
                CriterionResult(
                    criterion=criterion,
                    score=score,
                    reason=f"{criterion} 테스트 판단 근거",
                    source_ids=source_ids,
                )
                for criterion, score in zip(CRITERION_ORDER, self.scores)
            ],
            market_risks=["고객별 통합 비용 확인 필요"],
        )


class MarketEvaluationAgentTests(unittest.TestCase):
    def _agent(
        self,
        *,
        scores: tuple[int, int, int, int, int] = (5, 4, 3, 2, 1),
        pdf_evidence: bool = True,
        web_evidence: bool = True,
        unknown_source_id: bool = False,
    ):
        base_rag = FakeBaseRAG()
        market_rag = FakeMarketRAG(return_evidence=pdf_evidence)
        web_search = FakeWebSearch(return_evidence=web_evidence)
        backend = FakeEvaluationBackend(
            scores, unknown_source_id=unknown_source_id
        )
        agent = MarketEvaluationAgent(
            base_rag=base_rag,
            market_rag=market_rag,
            web_search=web_search,
            evaluation_backend=backend,
        )
        return agent, base_rag, market_rag, web_search, backend

    def test_selected_company_reaches_evaluation_and_calculates_weighted_score(self):
        """탐색 결과 ID가 같은 기업의 평가와 정확한 가중점수로 이어집니다."""

        agent, base_rag, market_rag, web_search, backend = self._agent()

        update = make_market_node(agent)({"company_id": " 17 "})
        result = update["market_evaluation"]

        self.assertEqual(base_rag.requested_ids, ["17"])
        self.assertEqual(backend.planned_company.company_id, "17")
        self.assertEqual(backend.evaluated_company.company_id, "17")
        self.assertEqual(result["company_id"], "17")
        self.assertEqual(result["company_name"], "테스트 로보틱스")
        self.assertEqual(
            [item["criterion"] for item in result["criteria"]],
            list(CRITERION_ORDER),
        )

        # 5·4·3·2·1점에 25·25·20·20·10%를 적용하면 67점입니다.
        self.assertEqual(result["market_score_100"], 67.0)
        self.assertEqual(result["investment_score_25"], 16.75)
        self.assertEqual(result["market_risks"], ["고객별 통합 비용 확인 필요"])

        # PDF는 chunk_id, 웹은 URL 기준으로 중복 제거되어 각각 하나만 남습니다.
        self.assertEqual(
            [item["source_id"] for item in result["evidence"]],
            ["P001", "W001"],
        )
        self.assertEqual(len(backend.received_evidence), 2)
        self.assertEqual(len(market_rag.queries), 3)
        self.assertTrue(all(limit == 3 for _, limit in market_rag.queries))
        self.assertEqual(
            web_search.received_queries,
            [["고객 계약", "제품 가격 ROI", "고객 도입 사례"]],
        )

    def test_limited_evidence_still_returns_numeric_score(self):
        """PDF·웹 근거가 없어도 정책대로 보수적인 숫자 점수를 유지합니다."""

        agent, *_ = self._agent(
            scores=(2, 2, 2, 2, 2),
            pdf_evidence=False,
            web_evidence=False,
        )
        result = agent.evaluate("17")

        self.assertEqual(result.market_score_100, 40.0)
        self.assertEqual(result.investment_score_25, 10.0)
        self.assertEqual(result.evidence, [])
        self.assertTrue(all(item.score == 2 for item in result.criteria))

    def test_web_timeout_falls_back_to_pdf_and_still_returns_score(self):
        """웹 검색 전체가 실패해도 PDF-only 평가 결과와 숫자 점수를 냅니다."""

        agent, base_rag, market_rag, _, backend = self._agent(
            scores=(3, 3, 3, 3, 3)
        )
        agent.web_search = FailingWebSearch()

        result = agent.evaluate("17")

        self.assertEqual(result.market_score_100, 60.0)
        self.assertEqual(result.investment_score_25, 15.0)
        self.assertEqual([item.source_id for item in result.evidence], ["P001"])
        self.assertEqual(
            [item.source_id for item in backend.received_evidence],
            ["P001"],
        )
        self.assertIn(
            "웹 검색 근거 수집에 실패하여 기업정보와 PDF 근거만으로 평가했습니다.",
            result.market_risks,
        )

    def test_partial_web_failure_is_disclosed_without_dropping_results(self):
        """부분 실패 시 성공한 웹 근거와 수집 한계를 함께 전달합니다."""

        agent, _, _, web_search, backend = self._agent()
        web_search.last_warnings = ["웹 검색 2/3 묶음 실패"]

        result = agent.evaluate("17")

        self.assertEqual(
            [item.source_id for item in backend.received_evidence],
            ["P001", "W001"],
        )
        self.assertIn(
            "일부 웹 검색이 실패하여 수집에 성공한 근거만으로 평가했습니다.",
            result.market_risks,
        )

    def test_unknown_company_is_rejected_before_research(self):
        agent, base_rag, *_ = self._agent()

        with self.assertRaisesRegex(MarketEvaluationError, "Unknown company_id"):
            agent.evaluate("999")

        self.assertEqual(base_rag.requested_ids, ["999"])

    def test_model_cannot_cite_an_unprovided_source_id(self):
        agent, *_ = self._agent(unknown_source_id=True)

        with self.assertRaisesRegex(MarketEvaluationError, "W999"):
            agent.evaluate("17")

    def test_graph_node_requires_selected_company_id(self):
        agent, *_ = self._agent()
        node = make_market_node(agent)

        for state in ({}, {"company_id": None}, {"company_id": "  "}):
            with self.subTest(state=state), self.assertRaisesRegex(
                ValueError, "nonempty"
            ):
                node(state)


class OpenAIWebSearchResilienceTests(unittest.TestCase):
    def test_queries_are_split_and_partial_results_are_preserved(self):
        search = FakeBatchingWebSearch(failed_first_queries={"q3"})

        results = search.search(["q1", "q2", "q3", "q4", "q5"])

        self.assertEqual(
            search.batches,
            [["q1", "q2"], ["q3", "q4"], ["q5"]],
        )
        self.assertEqual([item.query for item in results], ["q1 | q2", "q5"])
        self.assertEqual(search.last_warnings, ["웹 검색 2/3 묶음 실패"])

    def test_all_failed_batches_raise_for_agent_fallback(self):
        search = FakeBatchingWebSearch(failed_first_queries={"q1", "q3"})

        with self.assertRaisesRegex(WebSearchError, "All 2 web search batches"):
            search.search(["q1", "q2", "q3"])

        self.assertEqual(len(search.last_warnings), 2)

    def test_invalid_resilience_settings_are_rejected(self):
        invalid_options = (
            {"timeout_seconds": 0},
            {"max_retries": -1},
            {"max_queries_per_request": 0},
        )
        for options in invalid_options:
            with self.subTest(options=options), self.assertRaises(ValueError):
                OpenAIWebSearch(**options)


if __name__ == "__main__":
    unittest.main()
