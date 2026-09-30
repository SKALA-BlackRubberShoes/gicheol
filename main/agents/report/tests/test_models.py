"""보고서 입력 계약의 회귀 검증. 외부 API와 벡터 DB를 호출하지 않는다."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import unittest

from pydantic import ValidationError

from main.agents.report.schemas import (
    CompanyEvaluation,
    Evidence,
    InvestmentResult,
    ReportConfig,
    ReportInput,
    normalize_state,
)


class NormalizeStateTests(unittest.TestCase):
    def test_accumulated_results_do_not_mutate_input(self):
        state = {
            "results_by_company": {
                "mock-001": {
                    "company_record": {
                        "company_id": "mock-001",
                        "company_name": "가상 여울로보틱스",
                        "values": {"employees": 0},
                    },
                    "technical_result": {"summary": "합성 결과", "evidence_ids": ["r1"]},
                }
            },
            "messages": ["보고서와 무관한 그래프 상태"],
        }
        original = deepcopy(state)

        result = normalize_state(state)

        self.assertEqual(state, original)
        company = result.results_by_company["mock-001"]
        self.assertEqual(company.company_id, "mock-001")
        self.assertEqual(company.company_name, "가상 여울로보틱스")
        company.company_record["values"]["employees"] = 99
        company.technical_result.evidence_ids.append("r2")
        self.assertEqual(state, original)

    def test_single_company_graph_state(self):
        state = {
            "company_id": "mock-001",
            "company_name": "가상 여울로보틱스",
            "technical_result": {"status": "completed", "summary": "합성 기술 평가"},
            "investment_result": {
                "status": "completed", "decision": "추천", "final_score": 82
            },
            "config": {"is_mock": True},
            "retry_count": 2,
        }

        result = normalize_state(state)

        self.assertEqual(list(result.results_by_company), ["mock-001"])
        self.assertEqual(result.results_by_company["mock-001"].investment_result.final_score, 82)
        self.assertTrue(result.config.is_mock)

    def test_explicit_empty_accumulation_does_not_resurrect_previous_company(self):
        result = normalize_state({
            "results_by_company": {},
            "company_id": "previous-loop-company",
            "company_name": "이전 반복에서 선택된 기업",
            "no_candidates_reason": "적격 후보가 없습니다.",
        })

        self.assertEqual(result.results_by_company, {})
        self.assertEqual(result.no_candidates_reason, "적격 후보가 없습니다.")

    def test_accepts_nested_pydantic_models(self):
        company = CompanyEvaluation(
            company_id="mock-001", company_name="가상 기업",
            investment_result=InvestmentResult(status="completed", decision="추천", final_score=82),
        )
        evidence = Evidence(evidence_id="r1", title="합성 근거", is_mock=True)

        result = normalize_state({
            "config": ReportConfig(is_mock=True),
            "results_by_company": {"mock-001": company},
            "evidence": [evidence],
        })

        self.assertEqual(result.results_by_company["mock-001"], company)
        self.assertEqual(result.evidence, [evidence])
        self.assertIsNot(result.results_by_company["mock-001"], company)

    def test_accepts_report_input_model(self):
        original = ReportInput(results_by_company={
            "mock-001": CompanyEvaluation(company_id="mock-001", company_name="가상 기업")
        })

        result = normalize_state(original)

        self.assertEqual(result, original)
        self.assertIsNot(result, original)

    def test_rejects_mismatched_company_ids(self):
        cases = [
            {"company_id": "mock-002"},
            {"company_id": "mock-001", "company_record": {"company_id": "mock-002"}},
        ]
        for company in cases:
            with self.subTest(company=company), self.assertRaisesRegex(ValueError, "기업 ID 불일치"):
                normalize_state({"results_by_company": {"mock-001": company}})

    def test_rejects_invalid_status_and_nonfinite_scores(self):
        cases = [
            {"technical_result": {"status": "done"}},
            {"eligibility": {"status": "approved"}},
            {"investment_result": {"status": "done"}},
            {"investment_result": {"final_score": float("nan")}},
            {"investment_result": {"weighted_scores": {"technical": float("inf")}}},
            {"technical_result": {"scores": {"technical": float("nan")}}},
        ]
        for company in cases:
            with self.subTest(company=company), self.assertRaises(ValidationError):
                normalize_state({"results_by_company": {"mock-001": company}})

    def test_zero_and_null_scores_remain_distinct(self):
        result = normalize_state({"results_by_company": {
            "zero": {
                "technical_result": {"scores": {"verified": 0, "unknown": None}},
                "investment_result": {
                    "status": "completed", "decision": "보류", "final_score": 0,
                    "weighted_scores": {"technical": 0, "market": None},
                },
            },
            "unknown": {"investment_result": {"decision": "보류", "final_score": None}},
        }})

        zero = result.results_by_company["zero"]
        self.assertEqual(zero.investment_result.final_score, 0)
        self.assertEqual(zero.investment_result.weighted_scores["technical"], 0)
        self.assertIsNone(zero.investment_result.weighted_scores["market"])
        self.assertEqual(zero.technical_result.scores["verified"], 0)
        self.assertIsNone(zero.technical_result.scores["unknown"])
        self.assertIsNone(result.results_by_company["unknown"].investment_result.final_score)

    def test_sample_fixture_has_explicit_mock_data_and_unused_evidence(self):
        sample_path = Path(__file__).resolve().parents[4] / "examples/reports/report_sample_state.json"
        state = json.loads(sample_path.read_text(encoding="utf-8"))

        result = normalize_state(state)

        self.assertTrue(result.config.is_mock)
        self.assertTrue(all(company.is_mock for company in result.results_by_company.values()))
        self.assertTrue(all(item.is_mock for item in result.evidence))
        self.assertEqual(result.results_by_company["mock-001"].investment_result.final_score, 82)
        self.assertIsNone(result.results_by_company["mock-002"].investment_result.final_score)
        self.assertIn("mock-unused", {item.evidence_id for item in result.evidence})
        used_ids = set()
        for company in result.results_by_company.values():
            for key in ("eligibility", "technical_result", "market_result", "competition_result", "team_result", "investment_result"):
                section = getattr(company, key)
                if section is not None:
                    used_ids.update(section.evidence_ids)
                    for values in getattr(section, "score_evidence_ids", {}).values():
                        used_ids.update(values)
        self.assertNotIn("mock-unused", used_ids)
        self.assertEqual(len(used_ids), 4)


if __name__ == "__main__":
    unittest.main()
