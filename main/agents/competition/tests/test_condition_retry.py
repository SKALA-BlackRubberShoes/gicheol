"""Retry malformed comparison dimensions without weakening evidence gates."""

from copy import deepcopy
import json
import unittest

from main.agents.competition.agent import run_agent
from main.agents.competition.schemas import CompetitorComparison, SCORE_CRITERIA
from main.agents.competition.scoring import (
    ConditionChecksError,
    _validate_requested_structure,
)


DIMENSIONS = ("task", "metric", "protocol", "environment", "configuration", "stage")
TARGET = "시험 대상 기업"
COMPETITOR = "인덱스로보틱스"


def evidence(company):
    return [{
        "id": f"TEST-{company}", "company": company, "title": "가상 평가 근거",
        "locator": "fixture://comparison", "text": "테스트용 회사 자료",
        "is_mock": True,
    }]


def report_for(competitors=(COMPETITOR,)):
    return {
        "company": TARGET,
        "comparisons": [{
            "competitor": name, "verdict": "insufficient",
            "differentiation": "차이 미확인", "comparison_conditions": "조건 미확인",
            "like_for_like": False,
            "condition_checks": [
                {"dimension": dimension, "status": "unverified", "reason": "시험 근거 부족"}
                for dimension in DIMENSIONS
            ],
            "target_evidence_ids": [f"TEST-{TARGET}"],
            "competitor_evidence_ids": [f"TEST-{name}"], "caveat": "실제 평가 아님",
        } for name in competitors],
        "risks": [{
            "category": category, "description": "미확인", "status": "insufficient",
            "evidence_ids": [], "caveat": "자료 부족",
            "adoption_impact": "미확인", "scaling_impact": "미확인",
        } for category in ("technical", "operational", "legal")],
        "defensibility": {
            "statement": "미확인", "status": "insufficient",
            "evidence_ids": [], "caveat": "자료 부족",
        },
        "criterion_scores": [
            {"criterion": name, "rating": 0, "rationale": "근거 부족", "evidence_ids": []}
            for name in SCORE_CRITERIA
        ],
        "csv_assessment_scores": [
            {"criterion": name, "rating": 0, "rationale": "근거 부족",
             "evidence_ids": [], "source_fields": []}
            for name in SCORE_CRITERIA
        ],
        "rag_assessment_scores": [], "key_unknowns": ["테스트용 가상 응답"],
    }


def missing_stage(report, index=0):
    report["comparisons"][index]["condition_checks"].pop()


def duplicated_task(report, index=0):
    checks = report["comparisons"][index]["condition_checks"]
    checks[-1] = deepcopy(checks[0])


class SequenceModel:
    """Return supplied responses; any unexpected extra call fails immediately."""

    def __init__(self, *responses):
        self.responses = responses
        self.calls = []

    def with_structured_output(self, schema):
        if schema is not CompetitorComparison:
            raise AssertionError("The existing structured-output contract changed")
        return self

    def invoke(self, messages):
        self.calls.append(deepcopy(messages))
        index = len(self.calls) - 1
        if index >= len(self.responses):
            raise AssertionError("Unexpected additional model invocation")
        response = self.responses[index]
        if isinstance(response, Exception):
            raise response
        return deepcopy(response)


class ConditionRetryTests(unittest.TestCase):
    def setUp(self):
        self.state = {"company": TARGET, "competitors": [COMPETITOR], "sentinel": {"keep": True}}
        self.original_state = deepcopy(self.state)
        self.retrieval_calls = []

    def run_with(self, model):
        def search_company(company):
            self.retrieval_calls.append(company)
            return evidence(company)

        # Explicit evidence injection isolates these tests from CSV, PDF and APIs.
        return run_agent(self.state, object(), model=model, search_company=search_company)

    def assert_retry_context(self, model, bad_report, competitors):
        self.assertEqual(len(model.calls), 2)
        first, second = model.calls
        self.assertEqual(second[:len(first)], first)
        self.assertEqual(second[-2][0], "assistant")
        self.assertEqual(json.loads(second[-2][1]), bad_report)
        self.assertEqual(second[-1][0], "human")
        correction = second[-1][1]
        for name in (*DIMENSIONS, "missing", "duplicates", "count", "unverified", *competitors):
            self.assertIn(name, correction)
        self.assertRegex(correction, "[가-힣]")
        self.assertEqual(self.retrieval_calls, [TARGET, *competitors])

    def test_valid_response_does_not_retry_and_keeps_list_contract(self):
        model = SequenceModel(report_for())
        result = self.run_with(model)
        self.assertIs(result, self.state)
        self.assertEqual(len(model.calls), 1)
        checks = result["competitor_comparison"]["comparisons"][0]["condition_checks"]
        self.assertIsInstance(checks, list)
        self.assertEqual([item["dimension"] for item in checks], list(DIMENSIONS))
        self.assertEqual(result["competitor_score"]["total"], 0)

    def test_valid_supported_score_is_preserved(self):
        response = report_for()
        comparison = response["comparisons"][0]
        comparison.update(verdict="clear", like_for_like=True)
        for check in comparison["condition_checks"]:
            check["status"] = "aligned"
        for score in response["criterion_scores"][:2]:
            score.update(rating=2, evidence_ids=[f"TEST-{TARGET}", f"TEST-{COMPETITOR}"])
        model = SequenceModel(response)
        result = self.run_with(model)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(result["competitor_score"]["total"], 20)
        self.assertEqual([row["points"] for row in result["competitor_score"]["criteria"]], [10, 10, 0, 0])

    def test_missing_or_duplicate_dimension_gets_one_targeted_correction(self):
        for malformed in (missing_stage, duplicated_task):
            with self.subTest(malformed=malformed.__name__):
                self.setUp()
                bad = report_for()
                malformed(bad)
                model = SequenceModel(bad, report_for())
                result = self.run_with(model)
                self.assert_retry_context(model, bad, [COMPETITOR])
                self.assertEqual(result["competitor_score"]["total"], 0)
                self.assertEqual(result["competitor_comparison"]["comparisons"][0]["verdict"], "insufficient")

    def test_exhausted_retry_keeps_state_unchanged_and_preserves_diagnostics(self):
        bad = report_for()
        missing_stage(bad)
        model = SequenceModel(bad, bad)
        with self.assertRaisesRegex(ConditionChecksError, "after one correction retry") as caught:
            self.run_with(model)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(self.state, self.original_state)
        self.assertEqual(caught.exception.issues, [{
            "competitor": COMPETITOR, "missing": ["stage"], "duplicates": [], "count": 5,
        }])

    def test_correction_cannot_bypass_citation_ownership(self):
        bad = report_for()
        missing_stage(bad)
        corrected = report_for()
        corrected["comparisons"][0]["target_evidence_ids"] = [f"TEST-{COMPETITOR}"]
        model = SequenceModel(bad, corrected)
        with self.assertRaises(ValueError) as caught:
            self.run_with(model)
        self.assertNotIsInstance(caught.exception, ConditionChecksError)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(self.state, self.original_state)

    def test_correction_cannot_bypass_comparable_score_gate(self):
        bad = report_for()
        missing_stage(bad)
        corrected = report_for()
        corrected["criterion_scores"][1].update(
            rating=3, evidence_ids=[f"TEST-{TARGET}", f"TEST-{COMPETITOR}"],
        )
        model = SequenceModel(bad, corrected)
        with self.assertRaisesRegex(ValueError, "comparable evidence") as caught:
            self.run_with(model)
        self.assertNotIsInstance(caught.exception, ConditionChecksError)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(self.state, self.original_state)

    def test_unrelated_validation_failure_is_not_retried(self):
        bad = report_for()
        bad["comparisons"][0]["target_evidence_ids"] = ["UNKNOWN-ID"]
        model = SequenceModel(bad)
        with self.assertRaises(ValueError) as caught:
            self.run_with(model)
        self.assertNotIsInstance(caught.exception, ConditionChecksError)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(self.state, self.original_state)

    def test_provider_failure_is_not_treated_as_condition_error(self):
        model = SequenceModel(RuntimeError("provider unavailable"))
        with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
            self.run_with(model)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(self.state, self.original_state)

    def test_multiple_competitors_are_diagnosed_in_the_same_retry(self):
        competitors = [COMPETITOR, "추가 비교 기업"]
        self.state["competitors"] = competitors
        bad = report_for(competitors)
        missing_stage(bad, 0)
        duplicated_task(bad, 1)
        parsed = CompetitorComparison.model_validate(bad)
        with self.assertRaises(ConditionChecksError) as caught:
            _validate_requested_structure(parsed, TARGET, competitors)
        self.assertEqual(caught.exception.issues, [
            {"competitor": competitors[0], "missing": ["stage"], "duplicates": [], "count": 5},
            {"competitor": competitors[1], "missing": ["stage"], "duplicates": ["task"], "count": 6},
        ])
        model = SequenceModel(bad, report_for(competitors))
        result = self.run_with(model)
        self.assert_retry_context(model, bad, competitors)
        self.assertEqual(len(result["competitor_comparison"]["comparisons"]), 2)

    def test_complete_but_unaligned_conditions_are_not_retryable(self):
        bad = report_for()
        bad["comparisons"][0]["like_for_like"] = True
        model = SequenceModel(bad)
        with self.assertRaisesRegex(ValueError, "unaligned conditions") as caught:
            self.run_with(model)
        self.assertNotIsInstance(caught.exception, ConditionChecksError)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(self.state, self.original_state)


if __name__ == "__main__":
    unittest.main()
