"""외부 팀 원문 수집과 투자 판단 연결을 네트워크 없이 검증한다."""

from __future__ import annotations

import unittest

from main.agents.investment.agent import make_investment_judge_node
from main.agents.investment.example import sample_state
from main.agents.investment.team_research import TeamWebResearcher
from main.agents.market.schemas import WebSearchResult
from main.agents.investment.tests.test_agent import FakeModel, assessment


class FakeSearch:
    def __init__(self, hits):
        self.hits = hits
        self.queries = []

    def search(self, queries, *, max_results_per_query=5):
        self.queries.append(queries)
        return self.hits


class SequenceSearch(FakeSearch):
    def __init__(self, batches):
        super().__init__([])
        self.batches = batches

    def search(self, queries, *, max_results_per_query=5):
        self.queries.append(queries)
        return self.batches[len(self.queries) - 1]


def hit(url, *, content="모델이 만든 요약"):
    return WebSearchResult(query="팀", title="검색 제목", url=url, content=content,
                           content_kind="generated_summary")


def request():
    return {"company": {"id": "demo", "name": "가상 로보틱스 기업",
                        "base_rag": {"raw": {"홈페이지": "https://example-company.kr"}}},
            "queries": ["가상 로보틱스 기업 창업자"]}


class TeamWebResearchTests(unittest.TestCase):
    def test_fetched_original_page_becomes_company_owned_evidence(self):
        search = FakeSearch([hit("https://example-company.kr/team")])
        researcher = TeamWebResearcher(
            search, fetch_page=lambda url: (
                "가상 로보틱스 기업 팀", ["대표는 로봇 개발을 이끌고 있습니다."]
            ))

        result = researcher(request())

        self.assertEqual(len(result["evidence"]), 1)
        evidence = result["evidence"][0]
        self.assertEqual(evidence["source_type"], "company")
        self.assertEqual(evidence["company_id"], "demo")
        self.assertEqual(evidence["excerpt"], "대표는 로봇 개발을 이끌고 있습니다.")
        self.assertNotIn("모델이 만든 요약", str(evidence))
        self.assertEqual(result["team_info"]["evidence_ids"], [evidence["id"]])

    def test_other_company_and_unreadable_pages_are_excluded(self):
        search = FakeSearch([hit("https://news.example/other"), hit("https://news.example/unreadable")])

        def fetch(url):
            if url.endswith("unreadable"):
                raise OSError("blocked")
            return "다른 회사 창업자", ["다른 회사 대표의 경력입니다."]

        result = TeamWebResearcher(search, fetch_page=fetch)(request())
        self.assertEqual(result["evidence"], [])
        self.assertTrue(result["missing_items"])

    def test_company_only_results_trigger_external_source_search(self):
        search = SequenceSearch([
            [hit("https://example-company.kr/team")],
            [hit("https://news.example/interview")],
        ])

        def fetch(url):
            if "news.example" in url:
                return "가상 로보틱스 기업 창업자 인터뷰", ["대표가 제품 출시 경험을 설명했습니다."]
            return "가상 로보틱스 기업 팀", ["대표가 로봇 개발을 맡았습니다."]

        result = TeamWebResearcher(search, fetch_page=fetch)(request())
        self.assertEqual(len(search.queries), 2)
        self.assertIn("인터뷰", search.queries[1][0])
        self.assertEqual({item["source_type"] for item in result["evidence"]},
                         {"company", "independent"})

    def test_external_page_reaches_team_score_with_its_source_id(self):
        researcher = TeamWebResearcher(
            FakeSearch([hit("https://news.example/startup")]),
            fetch_page=lambda url: (
                "가상 로보틱스 기업 창업자 인터뷰",
                ["가상 로보틱스 기업 창업자는 로봇 제품 출시 경험이 있습니다."],
            ),
        )
        source_id = researcher(request())["evidence"][0]["id"]
        state = sample_state()
        state.pop("team_rating")
        state["current_candidate"]["team_info"] = {}
        state["evidence_registry"].pop("team")

        result = make_investment_judge_node(
            FakeModel(assessment(ref=source_id)), team_researcher=researcher,
        )(state)

        self.assertEqual(result["team_rating"]["score"], 8)
        self.assertEqual(result["evidence_registry"][source_id]["source_type"], "independent")
        self.assertIn(source_id, result["current_candidate"]["team_info"]["evidence_ids"])
        self.assertEqual(result["retry_count"], 1)


if __name__ == "__main__":
    unittest.main()
