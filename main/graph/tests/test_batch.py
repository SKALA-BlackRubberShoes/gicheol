"""CSV 전체 평가, 누적 저장, 순위와 단일 보고서 연결 검증."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from main.graph.batch import competitor_ids, csv_order, run_csv_ranking
from main.rag.company import BaseRAG


class BatchRankingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rag = BaseRAG()
        try:
            cls.records = rag.list_companies()
        finally:
            rag.close()

    def test_csv_order_and_competitors(self):
        ordered = csv_order(self.records)
        self.assertEqual([record.source.record_number for record in ordered],
                         sorted(record.source.record_number for record in self.records))
        self.assertEqual(len(competitor_ids(ordered[0], ordered)), 2)
        self.assertNotIn(ordered[0].company_id, competitor_ids(ordered[0], ordered))

    def test_all_companies_are_saved_before_one_report_even_if_winner_is_hold(self):
        ordered = csv_order(self.records)[:3]
        calls = []
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp)

            def evaluate(company_id, peers):
                calls.append(company_id)
                self.assertTrue(peers)
                self.assertIsNone(json.loads((destination / "ranking.json").read_text())["report"])
                score = {ordered[0].company_id: 70, ordered[1].company_id: 91,
                         ordered[2].company_id: 91}[company_id]
                return {"company_id": company_id, "current_evaluation": {
                    "candidate_id": company_id, "score": score,
                    "decision": "hold" if score == 91 else "invest",
                }, "technical_score": {"total": score}}

            def report(state, **options):
                self.assertEqual(calls, [record.company_id for record in ordered])
                self.assertEqual(options["allow_hold"], True)
                self.assertEqual(state["company_id"], ordered[1].company_id)
                return {"report_pdf_path": "winner.pdf"}

            data = run_csv_ranking(ordered, evaluate, report, destination)
            saved = json.loads((destination / "ranking.json").read_text())
            selected = json.loads((destination / "selected_report_input.json").read_text())
            self.assertEqual(data, saved)
            self.assertEqual(saved["status"], "completed")
            self.assertEqual(saved["evaluated_count"], 3)
            self.assertEqual([row["company_id"] for row in saved["ranking"][:2]],
                             [ordered[1].company_id, ordered[2].company_id])
            self.assertEqual(selected["current_evaluation"]["decision"], "hold")
            self.assertEqual(len(saved["companies"]), 3)

    def test_evaluation_failure_is_saved_and_next_company_is_ranked(self):
        ordered = csv_order(self.records)[:3]
        reports = []
        with tempfile.TemporaryDirectory() as temp:
            destination = Path(temp)

            def evaluate(company_id, peers):
                if company_id == ordered[1].company_id:
                    raise RuntimeError("provider unavailable")
                return {"company_id": company_id, "current_evaluation": {
                    "candidate_id": company_id,
                    "score": 95 if company_id == ordered[0].company_id else 96,
                    "decision": "invest"}}

            run_csv_ranking(ordered, evaluate,
                            lambda *a, **kw: reports.append(a) or {"report_pdf_path": "winner.pdf"},
                            destination)
            saved = json.loads((destination / "ranking.json").read_text())
            self.assertEqual(saved["status"], "completed")
            self.assertEqual(saved["attempted_count"], 3)
            self.assertEqual(saved["evaluated_count"], 2)
            self.assertEqual(saved["companies"][1]["error"]["message"], "provider unavailable")
            self.assertEqual(len(saved["ranking"]), 2)
            self.assertEqual(saved["winner_company_id"], ordered[2].company_id)
            self.assertEqual(len(reports), 1)

    def test_all_failures_leave_no_report(self):
        ordered = csv_order(self.records)[:2]
        with tempfile.TemporaryDirectory() as temp:
            def evaluate(company_id, peers):
                raise RuntimeError("unavailable")

            with self.assertRaisesRegex(ValueError, "총점이 null"):
                run_csv_ranking(ordered, evaluate,
                                lambda *a, **kw: self.fail("보고서가 호출되면 안 됩니다."), temp)
            saved = json.loads((Path(temp) / "ranking.json").read_text())
            self.assertEqual(saved["attempted_count"], 2)
            self.assertEqual(saved["evaluated_count"], 0)
            self.assertEqual(saved["status"], "no_scored_company")


if __name__ == "__main__":
    unittest.main()
