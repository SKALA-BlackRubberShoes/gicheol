"""BaseRAG 연결과 기존 세 분석 결과의 점수 변환을 검증합니다."""

from copy import deepcopy
from types import SimpleNamespace
import unittest

from main.agents.investment import make_rag_investment_judge_node, prepare_judge_state
from main.agents.investment.example import sample_state


class FakeRAG:
    def get_company(self, company_id):
        if company_id != "demo":
            return None
        data = {"company_id": "demo", "company_name": "가상 로보틱스 기업",
                "values": {"funding_stage": "Seed"}, "raw": {},
                "source": {"csv_path": "fixture.csv", "record_number": 2}}
        return SimpleNamespace(company_name=data["company_name"], model_dump=lambda: deepcopy(data))


class AdapterTests(unittest.TestCase):
    def test_preserves_explicit_analyses_and_input(self):
        state = {**sample_state(), "company_id": "demo"}
        state["technology_summary"] = {"company": "가상 로보틱스 기업", "detail": "기술 원본"}
        state["technical_score"] = {"total": 90, "max": 100, "status": "scored"}
        state["market_evaluation"] = {"company_id": "demo", "company_name": "가상 로보틱스 기업",
                                      "market_score_100": 80, "evidence": ["시장 원본"]}
        state["competitor_comparison"] = {"company": "가상 로보틱스 기업", "detail": "비교 원본"}
        state["competitor_score"] = {"total": 80, "max": 100, "status": "scored"}
        before = deepcopy(state)
        result = make_rag_investment_judge_node(FakeRAG())(state)
        self.assertEqual(state, before)
        self.assertEqual(result["total_score"], 83)
        self.assertEqual(result["next_action"], "report")
        payload = result["report_payload"]
        self.assertEqual(payload["company_id"], "demo")
        self.assertEqual(payload["company_data"], result["company_data"])
        self.assertEqual(payload["company"]["base_rag"]["source"]["record_number"], 2)
        self.assertEqual(payload["evaluation"]["scorecard"], result["scorecard"])
        for key in ("technology_summary", "technical_score", "market_evaluation",
                    "competitor_comparison", "competitor_score"):
            self.assertEqual(payload["source_outputs"][key], state[key])

    def test_threshold_blocks_report_handoff_but_preserves_inputs(self):
        state = {**sample_state(), "company_id": "demo"}
        state["decision_policy"] = {"recommend_min_score": 84}
        state["technical_score"] = {"total": 90, "max": 100}
        result = make_rag_investment_judge_node(FakeRAG())(state)
        self.assertEqual(result["total_score"], 83)
        self.assertEqual(result["decision"], "hold")
        self.assertIsNone(result["report_payload"])
        self.assertEqual(result["hold_payload"]["source_outputs"]["technical_score"], state["technical_score"])

    def test_main_results_are_mapped_without_inventing_evidence(self):
        state = {
            "company_id": "demo",
            "technology_summary": {"technology_summary": "가상 기술", "key_unknowns": []},
            "technical_score": {"total": 90, "max": 100, "criteria": [
                {"rationale": "기술 평가", "evidence_ids": ["CSV-demo"]}]},
            "competitor_comparison": {"key_unknowns": []},
            "competitor_score": {"total": 80, "max": 100, "criteria": [
                {"rationale": "경쟁 평가", "evidence_ids": ["CSV-demo"]}]},
            "market_evaluation": {"market_score_100": 80,
                                  "market_definition": "국내 로봇 시장", "criteria": [
                                      {"reason": "시장 평가", "source_ids": ["P001"]}]},
        }
        prepared = prepare_judge_state(FakeRAG(), state)
        self.assertEqual(prepared["technology_analysis"]["score"], 90)
        self.assertEqual(prepared["competition_analysis"]["score"], 80)
        self.assertEqual(prepared["market_analysis"]["score"], 80)
        result = make_rag_investment_judge_node(FakeRAG())(state)
        self.assertEqual(result["scorecard"]["technology"]["score"], 27)
        self.assertEqual(result["scorecard"]["market"]["score"], 24)
        self.assertEqual(result["decision"], "hold")
        self.assertEqual(result["evidence_registry"], {})
        self.assertEqual(result["total_score"], 75)
        self.assertIsNone(result["report_payload"])
        self.assertEqual(result["hold_payload"]["source_outputs"]["technical_score"], state["technical_score"])

    def test_provisional_scores_are_used_and_flagged_for_review(self):
        state = {
            "company_id": "demo",
            "technology_summary": {"key_unknowns": []},
            "technical_score": {"total": 25, "max": 100, "status": "provisional", "criteria": []},
            "competitor_comparison": {"key_unknowns": []},
            "competitor_score": {"total": 15, "max": 100, "status": "provisional", "criteria": []},
        }
        prepared = prepare_judge_state(FakeRAG(), state)
        self.assertEqual(prepared["technology_analysis"]["score"], 25)
        self.assertEqual(prepared["competition_analysis"]["score"], 15)
        self.assertIn("잠정 점수", prepared["technology_analysis"]["missing_items"][0])
        self.assertIn("잠정 점수", prepared["competition_analysis"]["missing_items"][0])

    def test_high_scores_do_not_require_separate_eligibility_or_team_evidence(self):
        state = {
            "company_id": "demo",
            "technology_summary": {"company": "가상 로보틱스 기업", "technology_summary": "기술 분석"},
            "technical_score": {"total": 95, "max": 100},
            "competitor_comparison": {"company": "가상 로보틱스 기업"},
            "competitor_score": {"total": 95, "max": 100},
            "market_evaluation": {"company_id": "demo", "company_name": "가상 로보틱스 기업",
                                  "market_score_100": 95, "market_definition": "국내 로봇 시장"},
        }
        result = make_rag_investment_judge_node(FakeRAG())(state)
        self.assertEqual(result["upstream_score_90"], 85.5)
        self.assertEqual(result["judge_score_10"], 0)
        self.assertEqual(result["total_score"], 85.5)
        self.assertEqual(result["decision"], "invest")
        self.assertEqual(result["current_evaluation"]["eligibility_status"], "unverified")
        self.assertEqual(result["report_payload"]["source_outputs"]["market_evaluation"], state["market_evaluation"])

    def test_csv_alone_cannot_establish_eligibility_or_scores(self):
        result = make_rag_investment_judge_node(FakeRAG())({"company_id": " demo "})
        self.assertEqual(result["company_id"], "demo")
        self.assertEqual(result["decision"], "hold")
        self.assertIsNone(result["total_score"])
        self.assertEqual(result["current_evaluation"]["eligibility_status"], "unverified")
        self.assertEqual(result["evidence_registry"], {})

    def test_rejects_mixed_company_state(self):
        for candidate in ({"id": "other"}, {"id": "demo", "name": "다른 기업"}):
            with self.subTest(candidate=candidate), self.assertRaises(ValueError):
                prepare_judge_state(FakeRAG(), {"company_id": "demo", "current_candidate": candidate})

        for field, value in (
            ("technology_summary", {"company": "다른 기업"}),
            ("competitor_comparison", {"company": "다른 기업"}),
            ("market_evaluation", {"company_id": "other"}),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                prepare_judge_state(FakeRAG(), {"company_id": "demo", field: value})

    def test_rejects_unknown_or_invalid_id(self):
        for company_id in (None, "", "  ", 1, "unknown"):
            with self.subTest(company_id=company_id), self.assertRaises(ValueError):
                prepare_judge_state(FakeRAG(), {"company_id": company_id})

    def test_provider_errors_are_not_treated_as_missing_data(self):
        class BrokenRAG:
            def get_company(self, company_id):
                raise RuntimeError("조회 실패")
        with self.assertRaises(RuntimeError):
            prepare_judge_state(BrokenRAG(), {"company_id": "demo"})


if __name__ == "__main__":
    unittest.main()
