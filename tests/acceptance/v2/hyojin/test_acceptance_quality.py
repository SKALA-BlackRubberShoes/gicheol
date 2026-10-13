"""SDD scenarios at the quality boundary; Supervisor/publish are not simulated."""

from copy import deepcopy
import importlib.util
from pathlib import Path
import unittest

from main.agents.quality.rubrics import CRITERIA, record

spec = importlib.util.spec_from_file_location("hyojin_acceptance_cases", Path(__file__).resolve().parents[3] / "fixtures/v2/hyojin/cases.py")
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)


class AcceptanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_at05_reprinted_promotions_fail_source_independence(self):
        case = f.fixture()
        original, reprint = case["sources"][:2]
        # Different URL and group, identical original document bytes.
        reprint["sha256"] = original["sha256"]
        reprint["content_ref"]["sha256"] = original["sha256"]
        case["bodies"][reprint["source_id"]] = case["bodies"][original["source_id"]]
        _, assessor, _, _ = f.services(case)
        verdicts = {record(v)["perspective"]: record(v) for v in await assessor(f.sufficiency_request(case))}
        market = verdicts["market"]
        self.assertFalse(market["structural_pass"])
        self.assertTrue(any(g["kind"] == "source_bias" for g in market["gaps"]))

    async def test_at05_valid_citation_with_unrelated_excerpt_fails(self):
        case = f.fixture()
        claim = case["perspective_results"]["market"]["claims"][0]
        claim["text"] = "Alpha has five million surgical robotics customers."
        f.rehash(case)
        verifier, assessor, _, _ = f.services(case)
        support = record(await verifier.verify(claim, evidence=case["evidence"], sources=case["sources"], company_id="alpha", as_of_date="2026-10-13"))
        self.assertEqual(support["support_status"], "unsupported")
        verdicts = {record(v)["perspective"]: record(v) for v in await assessor(f.sufficiency_request(case))}
        self.assertFalse(verdicts["market"]["structural_pass"])

    async def test_at06_recommendation_or_superiority_is_rewrite_without_research(self):
        for text in ("이 기업에 대한 투자 추천을 합니다.", "Alpha has technology superior to its rivals.",
                     "기술 종합 점수 95점이며 기업 순위 1위다.", "We recommend investing in Alpha."):
            with self.subTest(text=text):
                case = f.fixture()
                # Keep sufficient evidence intact; only expression is changed.
                case["report"]["sections"][0]["paragraphs"].append({
                    "paragraph_id": "P-recommendation", "text": text,
                    "kind": "connective", "claim_ids": [], "evidence_ids": []})
                f.rehash(case)
                judge = f.FixtureJudge()
                judge.scores = {criterion: 4 for criterion in CRITERIA}
                _, assessor, evaluator, _ = f.services(case, report_judge=judge)
                sufficient = await assessor(f.sufficiency_request(case))
                self.assertTrue(all(record(v)["structural_pass"] and record(v)["semantic_pass"] for v in sufficient))
                verdict = record(await evaluator(f.evaluator_request(case)))
                self.assertFalse(verdict["overall_pass"])
                self.assertEqual(verdict["repair_kind"], "rewrite")
                self.assertFalse(any(finding["repair_kind"] == "research" for finding in verdict["findings"]))
                self.assertTrue(all(finding["target_perspective"] is None for finding in verdict["findings"]))

    async def test_at07_unsupported_domain_claim_requests_domain_research(self):
        case = f.fixture()
        judge = f.FixtureJudge()
        cid = case["perspective_results"]["domain_application"]["claims"][1]["claim_id"]
        judge.support_status[cid] = "unsupported"
        _, _, evaluator, _ = f.services(case, verifier_judge=judge)
        verdict = record(await evaluator(f.evaluator_request(case)))
        self.assertEqual(verdict["repair_kind"], "research")
        self.assertTrue(any(finding["target_perspective"] == "domain_application" for finding in verdict["findings"]))

    async def test_at14_changed_report_is_bound_to_new_hash_revision_epoch(self):
        case = f.fixture()
        _, _, evaluator, _ = f.services(case)
        old = record(await evaluator(f.evaluator_request(case)))
        case["report"]["revision"] += 1
        case["report"]["evidence_epoch"] += 1
        case["report"]["sections"][6]["paragraphs"][0]["text"] += " Wider environmental tests remain unknown."
        f.rehash(case)
        new = record(await evaluator(f.evaluator_request(case)))
        self.assertTrue(new["overall_pass"])
        self.assertNotEqual((old["report_revision"], old["report_hash"], old["evidence_epoch"]),
                            (new["report_revision"], new["report_hash"], new["evidence_epoch"]))
        self.assertEqual(new["report_hash"], case["report"]["content_hash"])

    async def test_at16_two_research_targets_and_rewrite_all_survive(self):
        case = f.fixture()
        judge = f.FixtureJudge()
        for target in ("market", "domain_application"):
            judge.findings.append({"criterion": "groundedness", "severity": "critical", "section_id": target,
                                   "claim_id": None, "evidence_ids": [], "reason": f"Obtain additional {target} evidence",
                                   "repair_kind": "research", "target_perspective": target})
        judge.findings.append({"criterion": "neutrality", "severity": "critical", "section_id": "SUMMARY",
                               "claim_id": None, "evidence_ids": [], "reason": "Remove persuasive wording",
                               "repair_kind": "rewrite", "target_perspective": None})
        before = deepcopy(case)
        _, _, evaluator, _ = f.services(case, report_judge=judge)
        verdict = record(await evaluator(f.evaluator_request(case)))
        self.assertEqual(case, before)
        self.assertEqual(len(verdict["findings"]), 3)
        self.assertEqual({item["target_perspective"] for item in verdict["findings"] if item["repair_kind"] == "research"},
                         {"market", "domain_application"})
        self.assertEqual(verdict["repair_kind"], "research")

    async def test_at17_changed_result_hash_cannot_reuse_old_sufficiency(self):
        case = f.fixture()
        _, assessor, _, _ = f.services(case)
        old = await assessor(f.sufficiency_request(case))
        self.assertTrue(all(record(v)["semantic_pass"] for v in old))
        case["perspective_results"]["market"]["claims"][0]["text"] = "Changed assertion after gate approval"
        # Shared DTO validation rejects a changed payload under an old hash.
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            await assessor(f.sufficiency_request(case))
        # A correctly rehashed changed claim must still fail semantic support.
        f.rehash(case)
        new = {record(v)["perspective"]: record(v) for v in await assessor(f.sufficiency_request(case))}
        self.assertFalse(new["market"]["structural_pass"])
        self.assertTrue(any(g["kind"] == "citation_mismatch" for g in new["market"]["gaps"]))


if __name__ == "__main__":
    unittest.main()
