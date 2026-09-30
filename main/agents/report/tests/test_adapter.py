"""실제 투자 판단 payload에서 보고서까지의 오프라인 연결 검증."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pypdf import PdfReader

from main.agents.investment.adapter import make_rag_investment_judge_node
from main.agents.investment.example import sample_state
from main.agents.investment.scoring import judge_investment
from main.agents.report.adapter import adapt_project_state
from main.agents.report.agent import generate_report
from main.agents.report.content import build_document
from main.agents.report.schemas import normalize_state
from main.paths import PROJECT_ROOT
from main.rag.company import BaseRAG


class ReportAdapterTests(unittest.TestCase):
    def setUp(self):
        scratch = PROJECT_ROOT / "outputs" / "tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="report-adapter-", dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.output_dir = Path(self.temp.name)

    def test_normalized_contract_is_a_deep_copy_even_for_empty_results(self):
        state = {"results_by_company": {}, "company_id": "old-loop",
                 "report_payload": {"stale": True}, "config": {"is_mock": True}}
        result = adapt_project_state(state)
        self.assertEqual(result, state)
        self.assertIsNot(result, state)
        result["config"]["is_mock"] = False
        self.assertTrue(state["config"]["is_mock"])
        self.assertEqual(normalize_state(result).results_by_company, {})

    def test_real_judge_payload_preserves_scores_references_and_raw_audit(self):
        judged = judge_investment(sample_state())
        payload = judged["report_payload"]
        before = deepcopy(payload)

        result = adapt_project_state(payload)
        normalized = normalize_state(result)

        self.assertEqual(payload, before)
        self.assertEqual(result["source_payload"], payload)
        self.assertIsNot(result["source_payload"], payload)
        company = normalized.results_by_company["demo"]
        self.assertEqual(company.company_name, "가상 로보틱스 기업")
        self.assertEqual(company.investment_result.decision, "추천")
        self.assertEqual(company.investment_result.final_score, 83)
        self.assertEqual(company.investment_result.weighted_scores,
                         {"technical": 27, "market": 24, "competition": 24, "team": 8})
        self.assertEqual(company.technical_result.scores, {"technical": 90})
        self.assertEqual(company.market_result.scores, {"market": 80})
        self.assertEqual(company.team_result.scores["team"], 8)
        self.assertEqual(company.eligibility.status, "eligible")
        self.assertIn("가상 창업자", company.team_result.details["핵심 창업자"])
        self.assertIn("총점 80/100 이상", " ".join(company.investment_result.reasons))
        self.assertIn("판단 신뢰도", " ".join(company.investment_result.reasons))
        by_id = {item.evidence_id: item for item in normalized.evidence}
        self.assertEqual(by_id["technology"].doc_id, "document-technology")
        self.assertEqual(by_id["technology"].issuer, "가상 기관")
        self.assertEqual(by_id["technology"].quote, payload["evidence_registry"]["technology"]["excerpt"])
        self.assertEqual(result["source_payload"]["evidence_registry"]["technology"]["source_type"], "customer")

    def test_full_judge_state_and_direct_payload_are_equivalent(self):
        judged = judge_investment(sample_state())
        before = deepcopy(judged)

        result = adapt_project_state(judged)

        self.assertEqual(result, adapt_project_state(judged["report_payload"]))
        self.assertEqual(judged, before)

    def test_hold_uses_hold_payload_and_preserves_numeric_score(self):
        state = sample_state()
        state["decision_policy"] = {"recommend_min_score": 84}
        judged = judge_investment(state)
        self.assertIsNone(judged["report_payload"])

        result = normalize_state(adapt_project_state(judged))

        investment = result.results_by_company["demo"].investment_result
        self.assertEqual(investment.decision, "보류")
        self.assertEqual(investment.final_score, 83)
        self.assertEqual(investment.status, "completed")
        self.assertIn("84", " ".join(investment.reasons))

    def test_null_source_score_and_zero_team_are_not_confused(self):
        state = sample_state()
        state["technology_analysis"]["score"] = None
        state["team_rating"] = None
        judged = judge_investment(state)

        result = normalize_state(adapt_project_state(judged))

        company = result.results_by_company["demo"]
        self.assertIsNone(company.investment_result.final_score)
        self.assertIsNone(company.technical_result.scores["technical"])
        self.assertEqual(company.team_result.scores["team"], 0)
        self.assertEqual(company.investment_result.weighted_scores["team"], 0)
        self.assertEqual(company.investment_result.status, "insufficient")

    def test_unverified_eligibility_does_not_change_investment_decision(self):
        state = sample_state()
        state["current_candidate"].pop("eligibility")
        judged = judge_investment(state)

        result = normalize_state(adapt_project_state(judged))

        company = result.results_by_company["demo"]
        self.assertEqual(company.eligibility.status, "unknown")
        self.assertEqual(company.investment_result.decision, "추천")
        self.assertEqual(company.investment_result.status, "completed")

    def test_reconstructs_current_evaluation_state_without_handoff(self):
        judged = judge_investment(sample_state())
        expected = adapt_project_state(judged)
        judged.pop("report_payload")
        judged.pop("hold_payload")

        result = adapt_project_state(judged)

        self.assertEqual(result["results_by_company"], expected["results_by_company"])
        self.assertEqual(result["evidence"], expected["evidence"])

    def test_rejects_stale_route_ambiguous_payload_and_company_mismatch(self):
        judged = judge_investment(sample_state())
        cases = []
        stale = deepcopy(judged)
        stale["decision"] = "hold"
        cases.append(stale)
        ambiguous = deepcopy(judged)
        ambiguous["hold_payload"] = deepcopy(ambiguous["report_payload"])
        cases.append(ambiguous)
        wrong_company = deepcopy(judged)
        wrong_company["company_id"] = "another-company"
        cases.append(wrong_company)
        wrong_name = deepcopy(judged)
        wrong_name["current_candidate"]["name"] = "다른 기업"
        cases.append(wrong_name)
        stale_evaluation = deepcopy(judged)
        stale_evaluation["current_evaluation"]["score"] = 1
        cases.append(stale_evaluation)
        wrong_payload_id = deepcopy(judged["report_payload"])
        wrong_payload_id["evaluation"]["candidate_id"] = "another-company"
        cases.append(wrong_payload_id)
        for item in cases:
            with self.subTest(item=list(item)), self.assertRaises(ValueError):
                adapt_project_state(item)

    def test_rejects_source_output_identity_mismatch(self):
        for key, value in (
            ("technology_summary", {"company": "다른 기업"}),
            ("competitor_comparison", {"company": "다른 기업"}),
            ("market_evaluation", {"company_id": "another-company"}),
        ):
            payload = judge_investment(sample_state())["report_payload"]
            payload["source_outputs"][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "보고서 기업"):
                adapt_project_state(payload)

    def test_raw_outputs_enrich_text_but_metadata_is_not_validated_evidence(self):
        payload = judge_investment(sample_state())["report_payload"]
        payload["source_outputs"]["technology_summary"] = {
            "company": "가상 로보틱스 기업", "technology_summary": "가상 제품 설명",
            "summary_evidence_ids": ["metadata-only"],
            "problem_solution": {"problem": "반복 작업", "product_approach": "가상 로봇", "observed_outcome": "실증 미확인"},
            "performance_validations": [{"metric": "처리량", "result": "측정값 미확인", "test_conditions": "실험실"}],
            "sources": [{"id": "metadata-only", "title": "축약 출처", "locator": "https://example.test/meta"}],
        }
        payload["source_outputs"]["competitor_comparison"] = {
            "company": "가상 로보틱스 기업", "comparisons": [
                {"competitor": "비교용 가상 기업", "differentiation": "도구 교체 가능", "comparison_conditions": "실험실", "caveat": "현장 검증 필요"}
            ],
        }
        payload["source_outputs"]["market_evaluation"] = {
            "company_id": "demo", "company_name": "가상 로보틱스 기업",
            "market_definition": "가상 시장", "market_risks": ["가격 수용성 미확인"],
            "market_score_100": 80, "investment_score_25": 20,
        }

        result = normalize_state(adapt_project_state(payload))

        company = result.results_by_company["demo"]
        self.assertIn("반복 작업", company.technical_result.summary)
        self.assertIn("처리량", company.technical_result.summary)
        self.assertIn("비교용 가상 기업", company.competition_result.summary)
        self.assertIn("가격 수용성 미확인", company.market_result.risks)
        self.assertEqual(company.investment_result.weighted_scores["market"], 24)
        self.assertIn("metadata-only", company.technical_result.evidence_ids)
        metadata = next(item for item in result.evidence if item.evidence_id == "metadata-only")
        self.assertEqual(metadata.content_kind, "metadata_only")
        self.assertIsNone(metadata.is_mock)
        self.assertEqual(metadata.quote, "")
        self.assertNotIn("metadata-only", result.source_payload["evidence_registry"])
        self.assertEqual(result.source_payload["source_outputs"], payload["source_outputs"])

    def test_fullstate_market_and_csv_references_preserve_content_kind_without_changing_judgment(self):
        judged = judge_investment(sample_state())
        sources = judged["report_payload"]["source_outputs"]
        csv_source = {"id": "CSV-demo", "title": "기업 정보 CSV의 한 행",
                      "locator": "provided-companies.csv#record=18", "date": None, "is_mock": None}
        sources["technology_summary"] = {
            "company": "가상 로보틱스 기업", "technology_summary": "서지 연결 검증",
            "summary_evidence_ids": ["CSV-demo"], "sources": [deepcopy(csv_source)],
        }
        sources["competitor_comparison"] = {
            "company": "가상 로보틱스 기업", "sources": [{**csv_source, "company": "가상 로보틱스 기업"}],
        }
        sources["market_evaluation"] = {
            "company_id": "demo", "company_name": "가상 로보틱스 기업",
            "criteria": [{"criterion": "market_size", "reason": "검증용", "source_ids": ["PDF-1", "WEB-1"]}],
            "evidence": [
                {"source_id": "PDF-1", "source_type": "pdf", "content_kind": "source_excerpt",
                 "title": "회귀검증용 시장 보고서", "publisher": "검증 기관", "page": 12,
                 "url": "https://example.test/report.pdf", "published_at": "2026-01-01", "excerpt": "제공된 원문 발췌"},
                {"source_id": "WEB-1", "source_type": "web", "content_kind": "generated_summary",
                 "title": "회귀검증용 웹 결과", "publisher": None, "page": None,
                 "url": "https://example.test/web", "published_at": None, "excerpt": "상위 모델이 작성한 생성 요약"},
                {"source_id": "UNUSED", "source_type": "web", "title": "미인용 시장 자료", "excerpt": "미사용"},
            ],
        }
        before = deepcopy(judged)

        adapted = adapt_project_state(judged)
        normalized = normalize_state(adapted)
        catalog = {item.evidence_id: item for item in normalized.evidence}

        self.assertEqual(judged, before)
        self.assertEqual(adapted["source_payload"], before["report_payload"])
        self.assertNotIn("PDF-1", adapted["source_payload"]["evidence_registry"])
        self.assertEqual(normalized.results_by_company["demo"].investment_result.final_score, 83)
        self.assertEqual(catalog["CSV-demo"].content_kind, "metadata_only")
        self.assertEqual(catalog["CSV-demo"].csv_path, "provided-companies.csv")
        self.assertEqual(catalog["CSV-demo"].record_number, 18)
        self.assertIsNone(catalog["CSV-demo"].is_mock)
        self.assertEqual(catalog["PDF-1"].kind, "report")
        self.assertEqual(catalog["PDF-1"].page, 12)
        self.assertIn("page=발췌/제공 PDF 파일 기준", catalog["PDF-1"].provenance_note)
        self.assertEqual(catalog["PDF-1"].quote, "제공된 원문 발췌")
        self.assertEqual(catalog["PDF-1"].summary, "")
        self.assertEqual(catalog["WEB-1"].quote, "")
        self.assertEqual(catalog["WEB-1"].summary, "상위 모델이 작성한 생성 요약")
        document, warnings, _ = build_document(normalized)
        references = "\n".join(document["sections"][-1]["paragraphs"])
        for expected in ("CSV-demo", "PDF-1", "WEB-1", "제공 PDF p. 12", "서지정보만 제공", "AI 생성 요약·원문 인용 아님", "자료 유형 미확인"):
            self.assertIn(expected, references)
        self.assertNotIn("원문 p.", references)
        self.assertNotIn("UNUSED", references)
        self.assertFalse(any("출처를 찾을 수 없음" in value for value in warnings))
        result = generate_report(judged, output_dir=self.output_dir)
        text = "\n".join(page.extract_text() for page in PdfReader(result["report_pdf_path"]).pages)
        self.assertIn("회귀검증용 시장 보고서", text)
        self.assertIn("AI 생성 요약", text)

    def test_conflicting_supplemental_reference_ids_are_not_silently_overwritten(self):
        payload = judge_investment(sample_state())["report_payload"]
        payload["source_outputs"]["technology_summary"] = {
            "sources": [{"id": "same-id", "title": "자료 A", "locator": "https://example.test/a"}]}
        payload["source_outputs"]["competitor_comparison"] = {
            "sources": [{"id": "same-id", "title": "자료 B", "locator": "https://example.test/b"}]}
        with self.assertRaisesRegex(ValueError, "동일 근거 ID의 서지정보"):
            adapt_project_state(payload)

    def test_actual_baserag_adapter_pipeline_uses_local_csv_without_network(self):
        rag = BaseRAG()
        self.addCleanup(rag.close)
        record = rag.get_company("17")
        self.assertIsNotNone(record)
        state = {
            "company_id": "17",
            "technology_summary": {"company": record.company_name, "technology_summary": "연결 검증용 합성 기술 평가"},
            "technical_score": {"total": 95, "max": 100},
            "competitor_comparison": {"company": record.company_name},
            "competitor_score": {"total": 95, "max": 100},
            "market_evaluation": {"company_id": "17", "company_name": record.company_name,
                                  "market_score_100": 95, "investment_score_25": 23.75,
                                  "market_definition": "연결 검증용 합성 시장"},
        }
        before = deepcopy(state)
        with patch("socket.socket.connect", side_effect=AssertionError("unexpected network")) as network:
            judged = make_rag_investment_judge_node(rag, research_mode="off")(state)
            report = normalize_state(adapt_project_state(judged))

        network.assert_not_called()
        self.assertEqual(state, before)
        company = report.results_by_company["17"]
        self.assertEqual(company.company_record, record.model_dump())
        self.assertEqual(company.investment_result.final_score, 85.5)
        self.assertEqual(company.investment_result.weighted_scores["market"], 28.5)
        self.assertEqual(company.team_result.scores["team"], 0)
        self.assertEqual(company.investment_result.decision, "추천")
        self.assertEqual(company.eligibility.status, "unknown")

    def test_end_to_end_direct_and_fullstate_handoffs_generate_korean_pdf(self):
        judged = judge_investment(sample_state())
        before = deepcopy(judged)
        for value in (judged["report_payload"], judged):
            with self.subTest(full_state="current_evaluation" in value):
                result = generate_report(value, output_dir=self.output_dir)
                audit = json.loads(Path(result["report_json_path"]).read_text(encoding="utf-8"))
                self.assertEqual(audit["source_payload"], judged["report_payload"])
                self.assertEqual(audit["source_payload"]["evaluation"]["score"], 83)
                reader = PdfReader(result["report_pdf_path"])
                self.assertLessEqual(len(reader.pages), 5)
                text = "\n".join(page.extract_text() for page in reader.pages)
                self.assertIn("가상 로보틱스 기업", text)
                self.assertIn("추천", text)
                self.assertIn("83", text)
                self.assertIn("REFERENCE", text)
        self.assertEqual(judged, before)

    def test_hold_judgment_does_not_create_report_files(self):
        state = sample_state()
        state["decision_policy"] = {"recommend_min_score": 84}
        judged = judge_investment(state)
        before = list(self.output_dir.iterdir())
        for source in (judged, judged["hold_payload"]):
            with self.subTest(source="full" if source is judged else "payload"):
                with self.assertRaisesRegex(ValueError, "추천 판정 회사가 없어"):
                    generate_report(source, output_dir=self.output_dir)
                self.assertEqual(list(self.output_dir.iterdir()), before)

    def test_explicit_ranking_handoff_can_report_hold_without_changing_decision(self):
        state = sample_state()
        state["decision_policy"] = {"recommend_min_score": 84}
        judged = judge_investment(state)
        with patch("main.agents.report.agent.render_pdf", return_value=1):
            result = generate_report(judged, output_dir=self.output_dir, allow_hold=True)
        audit = json.loads(Path(result["report_json_path"]).read_text(encoding="utf-8"))
        self.assertEqual(audit["source_payload"]["evaluation"]["decision"], "hold")
        self.assertIn("보류", result["final_report"])
        self.assertIn("83", result["final_report"])

    def test_rejects_null_handoff_or_unjudged_project_state(self):
        for state in (None, {"company_id": "17", "report_payload": None, "hold_payload": None}):
            with self.subTest(state=state), self.assertRaises(ValueError):
                adapt_project_state(state)


if __name__ == "__main__":
    unittest.main()
