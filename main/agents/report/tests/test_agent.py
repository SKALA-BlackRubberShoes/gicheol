"""실제 PDF 생성과 LangGraph 연동을 검증하는 오프라인 통합 테스트."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from langchain_core.runnables import RunnableLambda
from pypdf import PdfReader

from main.agents.report.agent import OUTPUT_KEYS, build_report_graph, generate_report, make_report_node
from main.agents.report.schemas import SummarySelection


PROJECT_ROOT = Path(__file__).resolve().parents[4]


def sample_state() -> dict:
    return json.loads((PROJECT_ROOT / "examples/reports/report_sample_state.json").read_text(encoding="utf-8"))


class FakeSummaryModel:
    """LangChain 경계를 통과하되 외부 서비스에는 연결하지 않는 모델."""

    def __init__(self, fact_ids: list[str] | None = None, error: Exception | None = None):
        self.fact_ids = fact_ids or ["company-2"]
        self.error = error
        self.schema = None
        self.received_facts = None

    def with_structured_output(self, schema):
        self.schema = schema

        def respond(prompt):
            text = prompt.to_messages()[-1].content
            self.received_facts = json.loads(text[text.index("{"):])
            if self.error is not None:
                raise self.error
            return {"fact_ids": self.fact_ids}

        return RunnableLambda(respond)


class ReportAgentTests(unittest.TestCase):
    def setUp(self):
        scratch = PROJECT_ROOT / "outputs/reports/tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="agent-test-", dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.output_dir = Path(self.temp.name)
        # 로컬 .env나 사용자 환경의 추적 설정과 무관하게 외부 전송을 막는다.
        self.tracing = patch.dict(os.environ, {"LANGSMITH_TRACING": "false", "LANGCHAIN_TRACING_V2": "false"})
        self.tracing.start()
        self.addCleanup(self.tracing.stop)

    def read_audit(self, result):
        return json.loads(Path(result["report_json_path"]).read_text(encoding="utf-8"))

    def test_demo_generates_korean_pdf_with_ordered_sections_and_used_references(self):
        state = sample_state()
        original = deepcopy(state)

        result = generate_report(state, output_dir=self.output_dir)

        self.assertEqual(state, original)
        self.assertEqual(set(result), set(OUTPUT_KEYS))
        self.assertIn(result["report_status"], {"completed", "completed_with_warnings"})
        self.assertEqual(result["final_report"], Path(result["report_markdown_path"]).read_text(encoding="utf-8"))
        reader = PdfReader(result["report_pdf_path"])
        self.assertEqual(result["report_page_count"], len(reader.pages))
        self.assertGreaterEqual(len(reader.pages), 1)
        self.assertLessEqual(len(reader.pages), 5)
        text = "\n".join(page.extract_text() for page in reader.pages)
        self.assertIn("가상 여울로보틱스", text)
        self.assertIn("가상 별담오토메이션", text)
        self.assertIn("총점", text)
        self.assertIn("N/A", text)
        self.assertIn("SUMMARY", reader.pages[0].extract_text())
        self.assertIn("REFERENCE", reader.pages[-1].extract_text())
        self.assertLess(text.index("SUMMARY"), text.index("기업별 평가점수 비교표"))
        audit = self.read_audit(result)
        self.assertEqual(audit["pdf_status"], "completed")
        self.assertEqual(audit["summary_mode"], "deterministic")
        self.assertEqual(audit["document"]["sections"][-1]["title"], "REFERENCE")
        references = audit["document"]["sections"][-1]["paragraphs"]
        self.assertEqual(len(references), 4)
        self.assertNotIn("mock-unused", result["final_report"])
        self.assertNotIn("미사용 합성 자료", text)

    def test_valid_model_selection_uses_exact_existing_fact_text(self):
        model = FakeSummaryModel(["company-2"])

        result = generate_report(sample_state(), output_dir=self.output_dir, use_llm=True, model=model)

        audit = self.read_audit(result)
        self.assertIs(model.schema, SummarySelection)
        self.assertEqual(audit["summary_mode"], "llm_selection")
        self.assertIn(model.received_facts["company-2"], audit["document"]["summary"])
        self.assertNotIn(model.received_facts["company-1"], audit["document"]["summary"])
        self.assertFalse(any("SUMMARY 모델 호출/검증 실패" in warning for warning in result["report_warnings"]))

    def test_fabricated_fact_id_falls_back_and_preserves_original_facts(self):
        model = FakeSummaryModel(["fabricated-investment-result"])
        deterministic = generate_report(sample_state(), output_dir=self.output_dir)

        result = generate_report(sample_state(), output_dir=self.output_dir, use_llm=True, model=model)

        audit = self.read_audit(result)
        self.assertEqual(audit["summary_mode"], "fallback")
        self.assertEqual(audit["document"]["summary"], self.read_audit(deterministic)["document"]["summary"])
        self.assertTrue(any("SUMMARY 모델 호출/검증 실패 (ValueError)" in warning for warning in result["report_warnings"]))
        self.assertNotIn("fabricated-investment-result", result["final_report"])
        self.assertTrue(Path(result["report_pdf_path"]).is_file())

    def test_timeout_falls_back_without_leaking_provider_message(self):
        secret_marker = "private-response-body-and-key-never-print"
        model = FakeSummaryModel(error=TimeoutError(secret_marker))

        result = generate_report(sample_state(), output_dir=self.output_dir, use_llm=True, model=model)

        audit = self.read_audit(result)
        self.assertEqual(audit["summary_mode"], "fallback")
        self.assertTrue(any("SUMMARY 모델 호출/검증 실패 (TimeoutError)" in warning for warning in result["report_warnings"]))
        self.assertNotIn(secret_marker, json.dumps(audit))
        self.assertNotIn(secret_marker, result["final_report"])
        self.assertTrue(Path(result["report_pdf_path"]).is_file())

    def test_offline_mode_never_calls_model_or_network(self):
        model = FakeSummaryModel(error=AssertionError("offline mode called the model"))
        with patch("socket.socket.connect", side_effect=AssertionError("offline network connection")) as connect, \
                patch("socket.create_connection", side_effect=AssertionError("offline network connection")) as create_connection:
            result = generate_report(sample_state(), output_dir=self.output_dir, use_llm=False, model=model)

        connect.assert_not_called()
        create_connection.assert_not_called()
        self.assertIsNone(model.schema)
        self.assertIsNone(model.received_facts)
        self.assertEqual(self.read_audit(result)["summary_mode"], "deterministic")

    def test_team_node_only_returns_its_output_keys(self):
        state = sample_state()
        state["unrelated_team_result"] = {"keep": ["unchanged"]}
        original = deepcopy(state)
        node = make_report_node(output_dir=self.output_dir)

        result = node(state)

        self.assertEqual(set(result), set(OUTPUT_KEYS))
        self.assertNotIn("report_document", result)
        self.assertNotIn("results_by_company", result)
        self.assertNotIn("unrelated_team_result", result)
        self.assertEqual(state, original)

    def test_graph_reuse_generates_unique_directories_without_overwriting(self):
        graph = build_report_graph(output_dir=self.output_dir)
        state = sample_state()
        original = deepcopy(state)

        first = graph.invoke(state)
        first_pdf = Path(first["report_pdf_path"])
        first_bytes = first_pdf.read_bytes()
        second = graph.invoke(state)

        self.assertNotEqual(first_pdf.parent, Path(second["report_pdf_path"]).parent)
        self.assertEqual(first_pdf.read_bytes(), first_bytes)
        self.assertTrue(Path(second["report_pdf_path"]).is_file())
        self.assertEqual(first["final_report"], second["final_report"])
        self.assertEqual(state, original)

    def test_page_overflow_raises_and_leaves_failed_audit_and_complete_markdown(self):
        state = sample_state()
        repeated_evidence = "과도한 분석 근거 반복 " * 2000
        state["results_by_company"]["mock-001"]["technical_result"]["summary"] = repeated_evidence

        with self.assertRaisesRegex(ValueError, "PDF 생성 실패") as raised:
            generate_report(state, output_dir=self.output_dir)

        self.assertIn("5-page limit", str(raised.exception))
        self.assertIn("검토용 Markdown:", str(raised.exception))
        audits = list(self.output_dir.glob("*/RAG-Output.json"))
        self.assertEqual(len(audits), 1)
        audit = json.loads(audits[0].read_text(encoding="utf-8"))
        self.assertEqual(audit["pdf_status"], "failed")
        self.assertEqual(audit["pdf_error_type"], "ValueError")
        markdown = audits[0].with_suffix(".md").read_text(encoding="utf-8")
        self.assertIn(repeated_evidence.rstrip(), markdown)
        self.assertFalse(audits[0].with_suffix(".pdf").exists())


if __name__ == "__main__":
    unittest.main()
