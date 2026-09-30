"""공통 InvestmentState 안에서 보고서 결과를 보존하고 재선택 시 초기화한다."""
from pathlib import Path
import tempfile
import unittest

from langgraph.graph import START, END, StateGraph

from main.agents.investment import judge_investment
from main.agents.investment.example import sample_state
from main.graph import InvestmentState, make_report_node, make_start_node, new_request_state
from main.paths import DEFAULT_REPORT_DIR


class GraphIntegrationTests(unittest.TestCase):
    def setUp(self):
        scratch = DEFAULT_REPORT_DIR / "tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.directory.cleanup)

    def test_report_outputs_survive_common_state_graph(self):
        judgment = judge_investment(sample_state())
        judgment["company_id"] = judgment["current_candidate"]["id"]
        builder = StateGraph(InvestmentState)
        builder.add_node("report", make_report_node(output_dir=self.directory.name))
        builder.add_edge(START, "report")
        builder.add_edge("report", END)
        result = builder.compile().invoke(judgment)
        self.assertTrue(Path(result["report_pdf_path"]).is_file())
        self.assertGreater(result["report_page_count"], 0)
        self.assertIn("SUMMARY", result["final_report"])
        self.assertEqual(result["decision"], judgment["decision"])
        self.assertEqual(result["total_score"], judgment["total_score"])

    def test_company_reselection_clears_previous_report_paths(self):
        class Start:
            def invoke(self, state, config=None):
                return {"company_id": "new-company", "message": None}
        state = {"company_id": "old-company", "competitor_ids": ["competitor"],
                 "report_pdf_path": "previous-report.pdf", "final_report": "previous text",
                 "report_status": "completed", "report_warnings": ["old warning"]}
        result = make_start_node(Start())(state)
        self.assertIsNone(result["report_pdf_path"])
        self.assertIsNone(result["final_report"])
        self.assertIsNone(result["report_status"])
        self.assertEqual(result["report_warnings"], [])
        self.assertEqual(state["report_pdf_path"], "previous-report.pdf")

    def test_new_request_initializes_report_fields(self):
        result = new_request_state("로봇 기업 찾아줘")
        self.assertIsNone(result["report_pdf_path"])
        self.assertIsNone(result["report_status"])
        self.assertEqual(result["report_warnings"], [])


if __name__ == "__main__":
    unittest.main()
