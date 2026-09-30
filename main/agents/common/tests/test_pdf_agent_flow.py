"""Exercise graph nodes through retrieval, model payload, validation and totals."""
import json
import unittest
from unittest.mock import patch

from main.graph.nodes import make_comparison_node, make_technology_node
from main.rag.company import BaseRAG
from main.agents.common.csv_judgment import TECHNOLOGY_CRITERIA, COMPETITION_CRITERIA


class FakePDF:
    def __init__(self):
        self.calls = []

    def retrieve(self, company_id, company_name, role):
        self.calls.append((company_id, role))
        return [dict(id="PDF-"+company_id, company=company_name, title="시험용 제품 문서",
                     locator="test.pdf#page=1", text="시험용 모방학습 제품 및 현장 시연 설명",
                     document_id="TEST-"+company_id, page=1, evidence_scope="company_technical",
                     is_mock=True, limitations="테스트용 가상 자료")], {"role": role, "company_id": company_id}


class FakeLLM:
    def __init__(self):
        self.payloads = []

    def with_structured_output(self, schema):
        return self

    def invoke(self, messages):
        content = messages[-1][1]
        payload = json.loads(content[content.index("{"):])
        self.payloads.append(payload)
        technology = "company" in payload
        criteria = TECHNOLOGY_CRITERIA if technology else COMPETITION_CRITERIA
        company = payload.get("company", payload.get("target_company"))
        pdf = [row for row in payload["evidence"] if row.get("document_id")]
        target_ids = [row["id"] for row in pdf if row["company"] == company]
        all_ids = [row["id"] for row in pdf]
        result = {
            "company": company, "key_unknowns": ["가상 응답으로 배선 검증"],
            "criterion_scores": [dict(criterion=name, rating=0, rationale="엄격 근거 미확인", evidence_ids=[]) for name in criteria],
            "csv_assessment_scores": [dict(criterion=name, rating=0, rationale="CSV 단독 미확인", evidence_ids=[], source_fields=[]) for name in criteria],
            "rag_assessment_scores": [dict(criterion=name, rating=0 if "performance" in name else 2,
                rationale="가상 PDF 내용 기반 테스트", limitations="실제 평가 아님",
                evidence_ids=all_ids if name in {"differentiation", "comparable_performance"} else target_ids)
                for name in criteria],
        }
        if technology:
            result.update(
                technology_summary="가상 제품 요약", summary_evidence_ids=target_ids,
                problem_solution=dict(problem="미확인", product_approach="모방학습", observed_outcome="미확인",
                    customer_problem_quote="", baseline_or_goal_quote="", verdict="partial", evidence_ids=target_ids, caveat="미검증"),
                ai_role=dict(role="모방학습", status="insufficient", evidence_ids=target_ids, caveat="테스트"),
                performance_validations=[], claims=[], maturity="unknown", maturity_evidence_ids=[],
            )
        else:
            result.update(
                comparisons=[dict(competitor=name, verdict="insufficient", differentiation="제품 구성 차이",
                    comparison_conditions="조건 미확인", like_for_like=False,
                    condition_checks=[dict(dimension=d, status="unverified", reason="테스트 자료 없음")
                                      for d in ("task", "metric", "protocol", "environment", "configuration", "stage")],
                    target_evidence_ids=target_ids,
                    competitor_evidence_ids=[row["id"] for row in pdf if row["company"] == name], caveat="직접 비교 불가")
                    for name in payload["competitors"]],
                risks=[dict(category=c, description="미확인", status="insufficient", evidence_ids=[],
                            caveat="미확인", adoption_impact="미확인", scaling_impact="미확인")
                       for c in ("technical", "operational", "legal")],
                defensibility=dict(statement="미확인", status="insufficient", evidence_ids=[], caveat="미검증"),
            )
        return result


class AgentFlowTests(unittest.TestCase):
    def test_default_graph_path_delivers_company_pdfs_to_both_models(self):
        rag, pdf, model = BaseRAG(), FakePDF(), FakeLLM()
        state = {"company_id": "17", "competitor_ids": ["14"]}
        try:
            with patch("main.agents.common.pdf_evidence.default_pdf_rag", return_value=pdf):
                state.update(make_technology_node(rag, model)(state))
                state.update(make_comparison_node(rag, model)(state))
        finally:
            rag.close()
        self.assertEqual(pdf.calls, [("17", "technology"), ("17", "competition"), ("14", "competition")])
        self.assertEqual(len(model.payloads), 2)
        for score_key, report_key in [("technical_score", "technology_summary"), ("competitor_score", "competitor_comparison")]:
            self.assertEqual(state[score_key]["total"], 30)
            self.assertEqual(state[score_key]["verified_score"]["total"], 0)
            source_ids = {source["id"] for source in state[report_key]["sources"]}
            for criterion in state[score_key]["criteria"]:
                self.assertTrue(set(criterion["evidence_ids"]) <= source_ids)
            self.assertTrue(state[report_key]["rag_retrieval"])


if __name__ == "__main__":
    unittest.main()
