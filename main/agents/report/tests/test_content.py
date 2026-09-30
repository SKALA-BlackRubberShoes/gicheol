"""Verify report content preserves upstream decisions and evidence boundaries."""

from __future__ import annotations

from copy import deepcopy
import json
import unittest

from pydantic import ValidationError

from main.agents.report.content import apply_summary, build_document
from main.agents.report.schemas import normalize_state


def evidence(evidence_id: str = "source-1", **changes) -> dict:
    return {
        "evidence_id": evidence_id,
        "title": "원문에 있는 제목",
        "kind": "web",
        "issuer": "입력 제공 기관",
        "published_at": "2026-09-01",
        "url": f"https://example.com/{evidence_id}",
        **changes,
    }


def company(**changes) -> dict:
    return {
        "company_id": "company-1",
        "company_name": "검증 기업",
        "eligibility": {"status": "eligible", "reason": "앞선 에이전트가 적격으로 판단했습니다."},
        "investment_result": {
            "status": "completed", "decision": "보류", "final_score": 60,
            "reasons": ["검증 자료가 추가로 필요합니다."],
            "evidence_ids": ["source-1"],
            "score_evidence_ids": {"final_score": ["source-1"]},
        },
        **changes,
    }


def state(company_data: dict | None = None, **changes) -> dict:
    return {
        "config": {"as_of": "2026-09-30"},
        "results_by_company": {"company-1": company_data or company()},
        "evidence": [evidence()],
        **changes,
    }


def build(raw: dict) -> tuple[dict, list[str], dict[str, str]]:
    return build_document(normalize_state(raw))


def body(document: dict) -> str:
    return json.dumps(document, ensure_ascii=False)


def comparison_section(document: dict) -> dict:
    return next(item for item in document["sections"] if item["title"] == "기업별 평가점수 비교표")


def references(document: dict) -> list[str]:
    return document["sections"][-1]["paragraphs"]


class ContentTests(unittest.TestCase):
    def test_empty_results_describe_missing_input_without_inventing_candidates(self) -> None:
        document, warnings, facts = build({
            "config": {"as_of": "2026-09-30"},
            "results_by_company": {},
            "candidate_company_ids": ["candidate-awaiting-analysis"],
            "no_candidates_reason": "검색된 후보의 필수 공시가 없습니다.",
        })
        self.assertIn("평가 가능한 기업 결과가 없어", document["summary"])
        self.assertIn("검색된 후보의 필수 공시가 없습니다.", document["summary"])
        self.assertEqual(facts, {})
        self.assertTrue(any("평가 결과가 없는 후보: candidate-awaiting-analysis" in item for item in warnings))
        self.assertEqual(document["sections"][-1]["title"], "REFERENCE")
        self.assertIn("출처를 임의로 생성하지 않았습니다", references(document)[0])

    def test_all_hold_report_keeps_decisions_and_does_not_create_recommendation(self) -> None:
        document, _, facts = build(state())
        self.assertIn("추천 0개, 보류 1개", document["summary"])
        self.assertIn("추천 기업이 없습니다", document["summary"])
        self.assertIn("검증 기업: 보류, 총점 60", facts["company-1"])
        comparison = comparison_section(document)["tables"][0]["rows"][0]
        self.assertEqual(comparison[2:4], ["보류", "60"])

    def test_missing_results_and_missing_fields_are_visible(self) -> None:
        document, warnings, _ = build(state(company(
            investment_result=None,
            technical_result={
                "status": "failed", "missing_fields": ["특허 출원 번호"],
                "conflicts": ["성능 측정 조건이 다름"], "errors": ["원문 조회 실패"],
            },
        )))
        text = body(document)
        for expected in ("특허 출원 번호", "성능 측정 조건이 다름", "원문 조회 실패", "실행 실패", "N/A"):
            self.assertIn(expected, text)
        self.assertTrue(any("투자 판단 결과 미제공" in item for item in warnings))
        self.assertTrue(any("시장성 분석 결과 미제공" in item for item in warnings))
        self.assertEqual(comparison_section(document)["tables"][0]["rows"][0][2], "미확인")

    def test_only_referenced_catalog_entries_appear_in_reference(self) -> None:
        raw = state(evidence=[evidence(), evidence("unused", title="미사용 출처 제목")])
        document, _, _ = build(raw)
        refs = references(document)
        self.assertEqual(len(refs), 1)
        self.assertIn("근거 ID: source-1", refs[0])
        self.assertNotIn("미사용 출처 제목", body(document))
        self.assertIn("[1]", comparison_section(document)["tables"][0]["rows"][0][-1])

    def test_equal_duplicate_sources_are_deduplicated(self) -> None:
        raw = state()
        raw["results_by_company"]["company-1"]["investment_result"]["evidence"] = [evidence()]
        document, _, _ = build(raw)
        self.assertEqual(len(references(document)), 1)

    def test_conflicting_duplicate_source_id_is_rejected(self) -> None:
        raw = state(evidence=[evidence(), evidence(title="같은 ID의 다른 자료")])
        with self.assertRaisesRegex(ValueError, "동일한 evidence_id"):
            build(raw)

    def test_unresolved_citation_is_marked_and_never_fabricated(self) -> None:
        raw = state(evidence=[])
        document, warnings, _ = build(raw)
        self.assertIn("[근거 미확인: source-1]", body(document))
        self.assertIn("출처를 찾을 수 없음: source-1", warnings)
        self.assertIn("출처를 임의로 생성하지 않았습니다", references(document)[0])
        self.assertNotIn("https://example.com", body(document))

    def test_null_and_zero_are_distinct_for_scores_and_financial_data(self) -> None:
        raw = state(company(
            company_record={
                "raw": {"대표제품": "NULL"},
                "values": {"funding_latest_won": 0, "funding_total_won": None},
                "source": {"csv_path": "provided-companies.csv", "record_number": 2},
            },
            finance_data={"매출": None, "영업이익": 0},
            technical_result={"scores": {"zero": 0, "missing": None}},
            investment_result={"decision": "보류", "final_score": 0,
                               "weighted_scores": {"technical": 0, "market": None}},
        ))
        document, _, _ = build(raw)
        text = body(document)
        self.assertIn("zero 0; missing N/A", text)
        self.assertIn("최근 투자금 (원): 0", text)
        self.assertIn("매출: 미확인; 영업이익: 0", text)
        self.assertNotIn("핵심 제품: NULL", text)
        self.assertEqual(comparison_section(document)["tables"][0]["rows"][0][3], "0")

    def test_scores_are_not_recomputed_and_mismatch_is_reported(self) -> None:
        document, warnings, _ = build(state(company(investment_result={
            "status": "completed", "decision": "추천", "final_score": 70,
            "weighted_scores": {"technical": 30, "market": 30},
        })))
        self.assertEqual(comparison_section(document)["tables"][0]["rows"][0][2:4], ["추천", "70"])
        self.assertTrue(any("분야별 반영점수 합계와 총점 불일치 (전달값 유지)" in item for item in warnings))
        final_section = comparison_section(document)
        self.assertEqual([row[2] for row in final_section["tables"][1]["rows"]], ["30", "30"])

    def test_score_precision_is_preserved(self) -> None:
        document, _, facts = build(state(company(
            technical_result={"scores": {"기술 원점수": 21.123456789}},
            investment_result={"status": "completed", "decision": "보류", "final_score": 87.123456789},
        )))
        self.assertIn("21.123456789", body(document))
        self.assertIn("87.123456789", facts["company-1"])

    def test_inconsistent_recommendation_is_preserved_with_warning(self) -> None:
        document, warnings, _ = build(state(company(
            eligibility={"status": "ineligible", "reason": "필수 자격 불충족"},
            investment_result={"status": "insufficient", "decision": "추천", "final_score": None},
        )))
        self.assertEqual(comparison_section(document)["tables"][0]["rows"][0][2], "추천")
        self.assertTrue(any("추천 판정과 적격성/완료 상태/총점이 일치하지 않음" in item for item in warnings))

    def test_mock_inputs_are_conspicuously_labeled(self) -> None:
        variants = [
            state(config={"as_of": "2026-09-30", "is_mock": True}),
            state(company(is_mock=True)),
            state(evidence=[evidence(is_mock=True)]),
        ]
        for raw in variants:
            with self.subTest(raw=raw):
                document, _, _ = build(raw)
                self.assertTrue(document["summary"].startswith("[가상 자료 포함 / 제출용 실제 분석 아님]"))
                self.assertIn("가상 자료를 포함한 실행 예시", body(document))
        document, _, _ = build(state(evidence=[evidence(is_mock=True)]))
        self.assertIn("[가상 자료]", references(document)[0])

    def test_unused_mock_evidence_does_not_mark_real_content_as_mock(self) -> None:
        document, _, _ = build(state(evidence=[evidence(), evidence("unused", is_mock=True)]))
        self.assertNotIn("가상 자료", document["summary"])

    def test_invalid_csv_locator_zero_is_rejected(self) -> None:
        raw = state(evidence=[evidence(kind="csv", csv_path="provided.csv", record_number=0)])
        with self.assertRaises(ValidationError):
            build(raw)

    def test_generated_summary_cannot_be_stored_as_a_direct_quote(self) -> None:
        raw = state(evidence=[evidence(content_kind="generated_summary", quote="모델이 만든 문장")])
        with self.assertRaisesRegex(ValidationError, "원문 quote"):
            build(raw)

    def test_unknown_mock_flag_and_generated_summary_are_visible_in_references(self) -> None:
        document, warnings, _ = build(state(evidence=[evidence(
            content_kind="generated_summary", summary="상위 모델이 만든 문장", is_mock=None,
            provenance_note="원문 검증 미수행",
        )]))
        text = references(document)[0]
        self.assertIn("AI 생성 요약·원문 인용 아님", text)
        self.assertIn("자료 유형 미확인", text)
        self.assertIn("원문 검증 미수행", text)
        self.assertTrue(any("AI 생성 요약 출처" in value for value in warnings))
        self.assertNotIn("가상 자료 포함", document["summary"])

    def test_building_document_never_mutates_upstream_state(self) -> None:
        raw = state()
        before = deepcopy(raw)
        build(raw)
        self.assertEqual(raw, before)

    def test_repeated_decision_limitations_are_printed_once_with_both_owners(self) -> None:
        original_summary = "기술 원문 분석은 그대로 보존합니다."
        issue = "실증 조건과 표본 수 확인 필요"
        risk = "미해결 운영 위험"
        raw = state(company(
            technical_result={"summary": original_summary, "missing_fields": [issue]},
            investment_result={
                "status": "insufficient", "decision": "보류", "final_score": None,
                "reasons": [f"추가 검토 필요; {issue}; {risk}", "전달된 신뢰도: 낮음"],
                "missing_fields": [issue], "risks": [risk],
            },
        ))
        document, _, facts = build(raw)
        serialized = body(document)
        self.assertIn(original_summary, serialized)
        self.assertEqual(serialized.count(issue), 1)
        self.assertEqual(serialized.count(risk), 1)
        self.assertIn("기술·투자 판단 미확인", serialized)
        self.assertIn("추가 검토 필요", serialized)
        self.assertIn("전달된 신뢰도: 낮음", serialized)
        self.assertNotIn(issue, facts["company-1"])
        self.assertIn("종합 평가·한계 항목 참조", comparison_section(document)["tables"][0]["rows"][0][-1])

    def test_shared_reference_notes_keep_each_citation_and_locator(self) -> None:
        note = "상위 에이전트 제공 자료이며 원문 진위 재검증 안 함"
        raw = state(company(investment_result={
            "decision": "보류", "evidence_ids": ["source-1", "source-2"],
        }), evidence=[evidence(provenance_note=note, page=4),
                      evidence("source-2", provenance_note=note, page=8)])
        document, _, _ = build(raw)
        reference_text = "\n".join(references(document))
        self.assertEqual(reference_text.count(note), 1)
        for text in ("[1]", "[2]", "source-1", "source-2", "제공 위치 p. 4", "제공 위치 p. 8"):
            self.assertIn(text, reference_text)

    def test_source_used_only_in_risk_text_is_included_without_repeating_all_citations(self) -> None:
        raw = state(company(market_result={
            "summary": "시장 분석", "evidence_ids": ["P001"],
            "risks": ["가격 위험 (P002)", "출처 지정이 없는 별도 위험"],
        }), evidence=[evidence(), evidence("P001"), evidence("P002"), evidence("unused")])
        document, _, _ = build(raw)
        reference_text = "\n".join(references(document))
        self.assertIn("P002", reference_text)
        self.assertNotIn("unused", reference_text)
        risk_section = next(item for item in document["sections"] if item["title"] == "주요 위험과 대응 수준")
        explicit = next(item for item in risk_section["paragraphs"] if "가격 위험" in item)
        general = next(item for item in risk_section["paragraphs"] if "출처 지정이 없는" in item)
        self.assertIn("[3]", explicit)
        self.assertNotIn("[2]", general)

    def test_summary_selection_uses_supplied_sentences_and_rejects_unknown_ids(self) -> None:
        document, _, facts = build(state())
        original = deepcopy(document)
        result = apply_summary(document, facts, ["company-1"])
        self.assertEqual(result["summary"], original["summary"] + "\n" + facts["company-1"])
        self.assertEqual(document, original)
        with self.assertRaisesRegex(ValueError, "존재하지 않는 SUMMARY 사실 ID"):
            apply_summary(document, facts, ["made-up-fact"])


if __name__ == "__main__":
    unittest.main()
