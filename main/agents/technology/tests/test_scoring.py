"""출처 없는 모델 성능 항목을 점수·결과에서 제외하는지 확인합니다."""

import unittest

from main.agents.common.evidence import Evidence
from main.agents.technology.schemas import TechnologySummary
from main.agents.technology.scoring import _build_scorecard, _validate_citations


class PerformanceCitationTests(unittest.TestCase):
    def make_summary(self):
        summary = TechnologySummary.model_validate({
            "company": "테스트",
            "technology_summary": "제품 소개",
            "summary_evidence_ids": ["CSV-1"],
            "problem_solution": {
                "problem": "미확인", "product_approach": "제품 소개", "observed_outcome": "미확인",
                "customer_problem_quote": "", "baseline_or_goal_quote": "",
                "verdict": "insufficient", "evidence_ids": ["CSV-1"], "caveat": "미검증",
            },
            "ai_role": {"role": "미확인", "status": "insufficient", "evidence_ids": [], "caveat": "미검증"},
            "performance_validations": [{
                "metric": "자료 없음", "result": "자료 없음", "test_conditions": "자료 없음",
                "evidence_ids": [], "caveat": "미검증",
            }],
            "claims": [], "maturity": "unknown", "maturity_evidence_ids": [],
            "key_unknowns": [],
            "criterion_scores": [
                {"criterion": name, "rating": 0, "rationale": "자료 부족", "evidence_ids": []}
                for name in ("problem_solution", "ai_role", "performance_validation", "maturity")
            ],
            "csv_assessment_scores": [
                {"criterion": name, "rating": 0, "rationale": "자료 부족",
                 "evidence_ids": [], "source_fields": []}
                for name in ("problem_solution", "ai_role", "performance_validation", "maturity")
            ],
        })
        return summary

    def test_unattributed_performance_is_discarded(self):
        summary = self.make_summary()
        evidence = [Evidence(id="CSV-1", company="테스트", title="CSV", locator="fixture.csv#2", text="제품 소개")]
        _validate_citations(summary, evidence, "테스트")
        self.assertEqual(summary.performance_validations, [])
        self.assertIn("출처가 없는 성능 진술을 제외함", summary.key_unknowns)
        self.assertEqual(_build_scorecard(summary)["criteria"][2]["rating"], 0)

    def test_product_claim_cannot_become_measured_performance(self):
        from main.agents.technology.schemas import PerformanceValidation
        summary = self.make_summary()
        summary.performance_validations = [PerformanceValidation(
            metric="모델 생성 시간", result="하루 이내", test_conditions="작업 유형에 따라",
            evidence_ids=["CSV-1"], caveat="측정 표본 없음", measurement_basis="controlled_experiment",
        )]
        summary.criterion_scores[2].rating = 3
        summary.criterion_scores[2].evidence_ids = ["CSV-1"]
        records = [Evidence(id="CSV-1", company="테스트", title="제품 소개", locator="test.pdf#1",
                            text="하루 이내 모델 생성 가능", document_id="NOTE", is_derived=True)]
        _validate_citations(summary, records, "테스트")
        self.assertEqual(summary.performance_validations[0].measurement_basis, "unverified")
        self.assertEqual(_build_scorecard(summary)["criteria"][2]["rating"], 0)

    def test_unknown_stage_may_cite_a_document_explaining_missing_proof(self):
        summary = self.make_summary()
        summary.criterion_scores[3].evidence_ids = ["CSV-1"]
        records = [Evidence(id="CSV-1", company="테스트", title="소개", locator="test.pdf#1", text="개발 상태 미확인")]
        _validate_citations(summary, records, "테스트")
        self.assertEqual(_build_scorecard(summary)["criteria"][3]["rating"], 0)

    def test_empirical_technical_report_can_support_performance(self):
        from main.agents.technology.schemas import PerformanceValidation
        summary = self.make_summary()
        summary.performance_validations = [PerformanceValidation(
            metric="작업 성공률", result="8/10", test_conditions="로봇 A, 작업 B, 10회 반복",
            evidence_ids=["CSV-1"], caveat="단일 실험 환경", measurement_basis="controlled_experiment",
        )]
        summary.criterion_scores[2].rating = 3
        summary.criterion_scores[2].evidence_ids = ["CSV-1"]
        records = [Evidence(id="CSV-1", company="테스트", title="가상 실험 보고서", locator="test.pdf#1",
                            text="로봇 A 작업 B 10회 반복 성공 8회", document_id="REPORT",
                            evidence_scope="company_technical", is_mock=True)]
        _validate_citations(summary, records, "테스트")
        self.assertEqual(_build_scorecard(summary)["criteria"][2]["rating"], 3)


if __name__ == "__main__":
    unittest.main()
