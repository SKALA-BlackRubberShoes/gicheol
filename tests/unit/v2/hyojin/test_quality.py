"""Quality service regressions, with explicit fixture adapters."""

import asyncio
from copy import deepcopy
import importlib.util
from pathlib import Path
import unittest

from main.agents.quality.rubrics import (
    CLAIM_JUDGE_SCHEMA, REPORT_JUDGE_SCHEMA, SUFFICIENCY_JUDGE_SCHEMA,
    CRITERIA, QUESTIONS, LLMClientJudge, current_support, has_external_original,
    independent_groups, make_gap, merge_gaps, record,
)

spec = importlib.util.spec_from_file_location("hyojin_unit_cases", Path(__file__).resolve().parents[3] / "fixtures/v2/hyojin/cases.py")
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)


class ClaimTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.case = f.fixture()
        self.claim = self.case["claims"][0]
        self.judge = f.FixtureJudge()
        self.verifier, _, _, _ = f.services(self.case, verifier_judge=self.judge)

    async def verify(self):
        return record(await self.verifier.verify(self.claim, evidence=self.case["evidence"],
                                                sources=self.case["sources"], company_id="alpha", as_of_date="2026-10-13"))

    async def test_supported_claim_uses_current_complete_hashes(self):
        support = await self.verify()
        self.assertEqual(support["support_status"], "supported")
        self.assertEqual(support["claim_hash"], f.hash_fn(self.claim))
        self.assertTrue(current_support(support, self.claim, {e["evidence_id"]: e for e in self.case["evidence"]}, f.hash_fn))

    async def test_unrelated_excerpt_is_not_supported(self):
        self.claim["text"] = "Alpha is the leading provider of surgical robots."
        self.assertEqual((await self.verify())["support_status"], "unsupported")

    async def test_partial_support_does_not_count(self):
        self.judge.support_status[self.claim["claim_id"]] = "partial"
        self.assertEqual((await self.verify())["support_status"], "partial")

    async def test_unknown_never_counts_even_when_worker_says_supported(self):
        self.claim["kind"] = "unknown"
        self.assertEqual((await self.verify())["support_status"], "unverified")

    async def test_inference_without_conditions_fails(self):
        self.claim.update(kind="inference", limitations=[])
        self.assertEqual((await self.verify())["support_status"], "unverified")

    async def test_event_after_cutoff_fails_before_judge(self):
        self.case["evidence"][0]["event_date"] = "2026-10-14"
        self.assertEqual((await self.verify())["support_status"], "unverified")
        self.assertFalse(self.judge.calls)

    async def test_publication_after_cutoff_fails(self):
        self.case["sources"][0]["published_at"] = "2026-10-14"
        self.assertEqual((await self.verify())["support_status"], "unverified")

    async def test_unknown_publication_date_is_preserved(self):
        self.case["sources"][0]["published_at"] = None
        self.assertEqual((await self.verify())["support_status"], "supported")
        self.assertIsNone(self.judge.calls[0]["excerpts"][0]["source"]["published_at"])

    async def test_wrong_company_fails(self):
        self.case["evidence"][0]["company_id"] = "beta"
        self.assertEqual((await self.verify())["support_status"], "unverified")

    async def test_quote_hash_and_original_text_are_both_required(self):
        for mode in ("quote_hash", "body", "content_ref"):
            with self.subTest(mode=mode):
                case = f.fixture()
                if mode == "quote_hash":
                    case["evidence"][0]["quote_hash"] = "0" * 64
                elif mode == "body":
                    case["bodies"]["S-company"] = "Only a search snippet was returned"
                else:
                    case["sources"][0]["content_ref"] = None
                verifier, _, _, _ = f.services(case)
                result = await verifier.verify(case["claims"][0], evidence=case["evidence"], sources=case["sources"], company_id="alpha", as_of_date="2026-10-13")
                self.assertEqual(record(result)["support_status"], "unverified")

    async def test_unit_or_time_overstatement_rejected_by_semantic_oracle(self):
        for text in ("Alpha achieved 90 million dollars in revenue.",
                     "Alpha achieved 90 percent accuracy in all 2026 commercial deployments."):
            with self.subTest(text=text):
                self.claim["text"] = text
                self.assertEqual((await self.verify())["support_status"], "unsupported")

    async def test_verifier_exception_and_invalid_schema_fail(self):
        for response in ({}, {"support_status": "supported", "reason": "x", "supported_evidence_ids": ["invented"]},
                         {"support_status": "supported", "reason": "x" * 2001, "supported_evidence_ids": self.claim["evidence_ids"]}):
            with self.subTest(response=str(response)[:50]):
                self.judge.response = response
                self.assertEqual((await self.verify())["support_status"], "unverified")
        self.judge.response = None
        self.judge.error = RuntimeError("provider failed")
        self.assertEqual((await self.verify())["support_status"], "unverified")

    async def test_verifier_timeout_fails(self):
        async def slow(**kwargs):
            await asyncio.sleep(1)
        self.verifier.judge = slow
        self.verifier.timeout_seconds = 0.001
        self.assertEqual((await self.verify())["support_status"], "unverified")

    async def test_padded_citation_not_supported(self):
        self.judge.response = {"support_status": "supported", "reason": "Only one quote contributes", "supported_evidence_ids": []}
        self.assertEqual((await self.verify())["support_status"], "partial")

    async def test_stale_claim_evidence_and_verifier_bindings(self):
        support = await self.verify()
        evidence = {e["evidence_id"]: e for e in self.case["evidence"]}
        for field, value in (("text", "Changed text"), ("kind", "inference"), ("limitations", ["Changed scope"]), ("evidence_ids", [])):
            claim = deepcopy(self.claim)
            claim[field] = value
            self.assertFalse(current_support(support, claim, evidence, f.hash_fn))
        evidence[self.claim["evidence_ids"][0]]["quote"] = "Changed original excerpt"
        self.assertFalse(current_support(support, self.claim, evidence, f.hash_fn))
        support["verifier_version"] = "old-version"
        self.assertFalse(current_support(support, self.claim, evidence, f.hash_fn))


class SufficiencyTests(unittest.IsolatedAsyncioTestCase):
    async def assess(self, case, **kwargs):
        _, assessor, _, stored = f.services(case, **kwargs)
        return {record(v)["perspective"]: record(v) for v in await assessor(f.sufficiency_request(case))}, stored

    async def test_four_perspectives_pass_and_real_support_refs_saved(self):
        verdicts, stored = await self.assess(f.fixture())
        self.assertTrue(all(v["structural_pass"] and v["semantic_pass"] for v in verdicts.values()))
        self.assertEqual(len(stored), 8)
        for verdict in verdicts.values():
            self.assertEqual(verdict["result_hash"], f.semantic_hash_fn(verdict))
            self.assertTrue(all(ref["relative_path"] in stored for ref in verdict["claim_support_refs"]))

    async def test_missing_question_fails_separately_from_claim_count(self):
        case = f.fixture()
        del case["perspective_results"]["market"]["answered_questions"]["market.economics"]
        f.rehash(case)
        verdicts, _ = await self.assess(case)
        self.assertFalse(verdicts["market"]["structural_pass"])
        self.assertTrue(any(g["question"] == "market.economics" for g in verdicts["market"]["gaps"]))

    async def test_one_supported_claim_linked_to_two_questions_is_insufficient(self):
        case = f.fixture()
        judge = f.FixtureJudge()
        judge.support_status[case["perspective_results"]["market"]["claims"][1]["claim_id"]] = "partial"
        for q in QUESTIONS["market"]:
            case["perspective_results"]["market"]["answered_questions"][q] = [case["claims"][2]["claim_id"]]
        f.rehash(case)
        verdicts, _ = await self.assess(case, verifier_judge=judge)
        self.assertFalse(verdicts["market"]["structural_pass"])

    async def test_same_document_multiple_chunks_and_reprints_are_one_group(self):
        case = f.fixture()
        company, institute = case["sources"][:2]
        institute["sha256"] = company["sha256"]
        institute["content_ref"]["sha256"] = company["sha256"]
        case["bodies"]["S-institution"] = case["bodies"]["S-company"]
        verdicts, _ = await self.assess(case)
        self.assertFalse(verdicts["market"]["structural_pass"])
        self.assertTrue(any(g["kind"] == "source_bias" for g in verdicts["market"]["gaps"]))

    async def test_company_controlled_sources_do_not_satisfy_external_rule(self):
        case = f.fixture()
        for source in case["sources"]:
            source["corporate_controlled"] = True
        verdicts, _ = await self.assess(case)
        self.assertFalse(any(v["structural_pass"] for v in verdicts.values()))

    async def test_global_three_groups_rule(self):
        case = f.fixture()
        case["sources"][2]["publisher_group"] = case["sources"][1]["publisher_group"]
        verdicts, _ = await self.assess(case)
        self.assertFalse(any(v["structural_pass"] for v in verdicts.values()))

    async def test_missing_limitations_and_critical_gap_fail(self):
        for mode in ("limitations", "critical"):
            case = f.fixture()
            result = case["perspective_results"]["market"]
            if mode == "limitations":
                result["limitations"] = []
            else:
                result["gaps"] = [make_gap("market", "missing_evidence", "Verify demand")]
            f.rehash(case)
            verdicts, _ = await self.assess(case)
            self.assertFalse(verdicts["market"]["structural_pass"])

    async def test_eligibility_critical_or_unsupported_blocks_every_view(self):
        for mode in ("critical", "unsupported", "future"):
            case = f.fixture()
            if mode == "critical":
                case["eligibility"]["gaps"] = [make_gap("eligibility", "eligibility_unknown", "Confirm exit status")]
            elif mode == "unsupported":
                case["eligibility"]["claims"][0]["text"] = "An unsupported target assumption"
            else:
                case["evidence"][-1]["event_date"] = "2026-10-14"
            f.rehash(case)
            verdicts, _ = await self.assess(case)
            self.assertFalse(any(v["structural_pass"] for v in verdicts.values()))
            self.assertTrue(all(any(g["perspective"] == "eligibility" and g["severity"] == "critical"
                                    for g in v["gaps"]) for v in verdicts.values()))

    async def test_target_support_failure_keeps_scope_claim_evidence_and_reason(self):
        for status in ("unsupported", "partial", "unverified"):
            with self.subTest(status=status):
                case = f.fixture()
                judge = f.FixtureJudge()
                if status == "unverified":
                    case["evidence"][-1]["event_date"] = "2026-10-14"
                    expected_reason = "event_date is after the assessment cutoff"
                else:
                    judge.support_status["C-eligibility"] = status
                    expected_reason = "Synthetic fixture entailment verdict"
                verdicts, stored = await self.assess(case, verifier_judge=judge)
                identities = set()
                for verdict in verdicts.values():
                    gaps = [g for g in verdict["gaps"] if g["kind"] == "eligibility_unknown"]
                    self.assertEqual(len(gaps), 1)
                    gap = gaps[0]
                    self.assertEqual(gap["perspective"], "eligibility")
                    self.assertEqual(gap["evidence_ids"], ["E-eligibility"])
                    self.assertIn("C-eligibility", gap["question"])
                    self.assertIn(expected_reason, gap["question"])
                    identities.add(gap["gap_id"])
                self.assertEqual(len(identities), 1)
                self.assertNotIn("C-eligibility", {support["claim_id"] for support in stored.values()})

    async def test_declared_target_critical_gap_is_preserved(self):
        case = f.fixture()
        original = make_gap("eligibility", "conflicting_evidence", "Confirm conflicting exit reports",
                            evidence_ids=["E-eligibility"], query_hints=["Exit registry", "Official target record"])
        case["eligibility"]["gaps"] = [original]
        f.rehash(case)
        verdicts, _ = await self.assess(case)
        self.assertTrue(all(original in verdict["gaps"] for verdict in verdicts.values()))

    async def test_unknown_target_condition_and_missing_claims_have_target_gaps(self):
        for mode in ("condition", "missing_claims"):
            case = f.fixture()
            if mode == "condition":
                case["eligibility"].update(funding_stage="unknown", verdict="unknown")
                expected = "funding_stage"
            else:
                case["eligibility"]["claims"] = []
                expected = "Provide original evidence"
            f.rehash(case)
            verdicts, _ = await self.assess(case)
            for verdict in verdicts.values():
                self.assertFalse(verdict["structural_pass"])
                self.assertTrue(any(g["perspective"] == "eligibility" and expected in g["question"] for g in verdict["gaps"]))

    async def test_target_warning_alone_does_not_block_valid_support(self):
        case = f.fixture()
        case["eligibility"]["gaps"] = [make_gap("eligibility", "missing_evidence", "Optional registry follow-up", severity="warning")]
        f.rehash(case)
        verdicts, _ = await self.assess(case)
        self.assertTrue(all(v["structural_pass"] and v["semantic_pass"] for v in verdicts.values()))

    async def test_missing_or_unverified_rag_receipt_fails(self):
        for mode in ("missing", "company", "corpus", "index", "chunk", "provider"):
            case = f.fixture()
            _, assessor, _, _ = f.services(case)
            receipt = case["perspective_results"]["technology_maturity"]["retrieval_receipts"][0]
            if mode == "missing":
                case["perspective_results"]["technology_maturity"]["retrieval_receipts"] = []
            elif mode == "company":
                receipt["filters"]["company_id"] = "beta"
            elif mode in {"corpus", "index"}:
                receipt[f"{mode}_version"] = "different-version"
            elif mode == "chunk":
                receipt["ranked_hits"][0]["chunk_id"] = "different-chunk"
            else:
                receipt["provider"] = "self-declared-search"
            f.rehash(case)
            verdicts = {record(v)["perspective"]: record(v) for v in await assessor(f.sufficiency_request(case))}
            self.assertFalse(verdicts["technology_maturity"]["structural_pass"], mode)

    async def test_semantic_judge_schema_exception_and_timeout_fail(self):
        for mode in ("schema", "exception", "timeout"):
            judge = f.FixtureJudge()
            if mode == "schema":
                judge.response = {"semantic_pass": True}
            elif mode == "exception":
                judge.error = RuntimeError("judge failed")
            else:
                async def judge(**kwargs):
                    await asyncio.sleep(1)
            verdicts, _ = await self.assess(f.fixture(), sufficiency_judge=judge, timeout_seconds=0.001 if mode == "timeout" else 1)
            self.assertFalse(any(v["semantic_pass"] for v in verdicts.values()))

    async def test_conflict_original_text_and_warning_are_preserved(self):
        case = f.fixture()
        result = case["perspective_results"]["market"]
        warning = make_gap("market", "conflicting_evidence", "Difference in test scope explains the reported conflict",
                           evidence_ids=result["evidence_ids"], severity="warning", query_hints=["scope"])
        result["gaps"] = [warning]
        f.rehash(case)
        judge = f.FixtureJudge()
        verdicts, _ = await self.assess(case, sufficiency_judge=judge)
        self.assertIn(warning, verdicts["market"]["gaps"])
        market = next(payload for payload in judge.calls if payload["perspective"] == "market")
        self.assertEqual(len([ex for ex in market["original_excerpts"] if "conflicting_evidence" in ex]), 2)

    async def test_mutable_inputs_are_not_changed(self):
        case = f.fixture()
        before = deepcopy(case)
        await self.assess(case)
        self.assertEqual(case, before)


class ReportTests(unittest.IsolatedAsyncioTestCase):
    async def evaluate(self, case, judge=None, **kwargs):
        _, _, evaluator, _ = f.services(case, report_judge=judge, **kwargs)
        return record(await evaluator(f.evaluator_request(case)))

    async def test_valid_report_passes_and_binds_revision_hash_epoch(self):
        case = f.fixture()
        verdict = await self.evaluate(case)
        self.assertTrue(verdict["overall_pass"])
        self.assertEqual(verdict["repair_kind"], "none")
        self.assertEqual((verdict["report_revision"], verdict["report_hash"], verdict["evidence_epoch"]),
                         (3, case["report"]["content_hash"], 4))

    async def test_each_score_must_pass_and_bool_is_not_an_integer(self):
        for criterion in CRITERIA:
            judge = f.FixtureJudge()
            judge.scores[criterion] = 2
            verdict = await self.evaluate(f.fixture(), judge)
            self.assertFalse(verdict["overall_pass"])
        for value in (True, "3", 5, -1):
            judge = f.FixtureJudge()
            judge.scores["neutrality"] = value
            verdict = await self.evaluate(f.fixture(), judge)
            self.assertFalse(verdict["structural_checks"]["judge_response"])

    async def test_critical_finding_blocks_high_scores(self):
        judge = f.FixtureJudge()
        judge.scores = {criterion: 4 for criterion in CRITERIA}
        judge.findings = [self.finding("neutrality", "SUMMARY", "rewrite")]
        self.assertFalse((await self.evaluate(f.fixture(), judge))["overall_pass"])

    def finding(self, criterion, section, repair, target=None):
        return {"criterion": criterion, "severity": "critical", "section_id": section,
                "claim_id": None, "evidence_ids": [], "reason": "Synthetic fixture requires repair",
                "repair_kind": repair, "target_perspective": target}

    async def test_all_findings_preserved_and_research_precedes_rewrite(self):
        judge = f.FixtureJudge()
        judge.findings = [self.finding("groundedness", "market", "research", "market"),
                          self.finding("groundedness", "domain_application", "research", "domain_application"),
                          self.finding("neutrality", "SUMMARY", "rewrite")]
        verdict = await self.evaluate(f.fixture(), judge)
        self.assertEqual(verdict["findings"], judge.findings)
        self.assertEqual(verdict["repair_kind"], "research")

    async def test_judge_failure_schema_and_unknown_finding_fail_closed(self):
        for mode in ("exception", "schema", "criterion", "section", "evidence", "target"):
            judge = f.FixtureJudge()
            if mode == "exception":
                judge.error = RuntimeError("provider exception")
            elif mode == "schema":
                judge.response = {"overall_pass": True}
            elif mode == "criterion":
                del judge.scores["neutrality"]
            else:
                finding = self.finding("groundedness", "SUMMARY", "rewrite")
                finding[{"section": "section_id", "evidence": "evidence_ids", "target": "target_perspective"}[mode]] = ["unknown"] if mode == "evidence" else "unknown"
                judge.findings = [finding]
            verdict = await self.evaluate(f.fixture(), judge)
            self.assertFalse(verdict["overall_pass"], mode)
            self.assertFalse(verdict["structural_checks"]["judge_response"], mode)

    async def test_judge_timeout_fails(self):
        async def slow(**kwargs):
            await asyncio.sleep(1)
        verdict = await self.evaluate(f.fixture(), slow, timeout_seconds=0.001)
        self.assertFalse(verdict["overall_pass"])

    async def test_reference_set_must_match_body(self):
        case = f.fixture()
        case["report"]["sections"][-1]["paragraphs"].pop()
        f.rehash(case)
        self.assertFalse((await self.evaluate(case))["structural_checks"]["reference_integrity"])

    async def test_reference_evidence_ids_cannot_replace_bibliographic_text(self):
        case = f.fixture()
        for paragraph in case["report"]["sections"][-1]["paragraphs"]:
            paragraph["text"] = "Reference unavailable."
        f.rehash(case)
        verdict = await self.evaluate(case)
        self.assertFalse(verdict["overall_pass"])
        self.assertFalse(verdict["structural_checks"]["reference_format"])

    async def test_unknown_reference_publication_date_must_be_explicit(self):
        case = f.fixture()
        case["sources"][0]["published_at"] = None
        self.assertFalse((await self.evaluate(case))["structural_checks"]["reference_format"])
        case["report"]["sections"][-1]["paragraphs"][0]["text"] = case["report"]["sections"][-1]["paragraphs"][0]["text"].replace("(2026)", "(발행일 미상)")
        f.rehash(case)
        self.assertTrue((await self.evaluate(case))["overall_pass"])

    async def test_title_only_perspective_requires_rewrite_from_existing_evidence(self):
        case = f.fixture()
        empty = case["report"]["sections"][3]
        removed = {p["paragraph_id"] for p in empty["paragraphs"]}
        empty["paragraphs"] = []
        case["report"]["citations"] = [c for c in case["report"]["citations"] if c["paragraph_id"] not in removed]
        case["report"]["claim_ids"] = list(dict.fromkeys(cid for s in case["report"]["sections"] for p in s["paragraphs"] for cid in p["claim_ids"]))
        f.rehash(case)
        verdict = await self.evaluate(case)
        self.assertFalse(verdict["overall_pass"])
        self.assertEqual(verdict["repair_kind"], "rewrite")

    async def test_uncited_evidence_is_supplied_to_judge(self):
        case = f.fixture()
        judge = f.FixtureJudge()
        await self.evaluate(case, judge)
        self.assertEqual(len(judge.calls[0]["all_original_evidence"]), len(case["evidence"]))

    async def test_unresolved_uncited_contrary_evidence_cannot_pass(self):
        case = f.fixture()
        ev = deepcopy(case["evidence"][0])
        ev.update(evidence_id="E-contrary", chunk_id="contrary-chunk", quote="Contrary study reports failed deployment")
        ev["quote_hash"] = f.quote_hash(ev["quote"])
        case["evidence"].append(ev)
        verdict = await self.evaluate(case)
        self.assertFalse(verdict["overall_pass"])
        self.assertFalse(verdict["structural_checks"]["bias_control"])

    async def test_recommendation_in_section_title_cannot_pass(self):
        case = f.fixture()
        case["report"]["sections"][2]["title"] = "기업 기술 순위"
        f.rehash(case)
        verdict = await self.evaluate(case)
        self.assertFalse(verdict["overall_pass"])
        self.assertEqual(verdict["repair_kind"], "rewrite")

    async def test_report_input_is_immutable_to_judge(self):
        case = f.fixture()
        before = deepcopy(case)
        await self.evaluate(case)
        self.assertEqual(case, before)


class ProvenanceTests(unittest.TestCase):
    def test_corporate_reprint_is_not_external_independent_proof(self):
        sources = f.fixture()["sources"]
        sources[1]["sha256"] = sources[0]["sha256"]
        sources[2]["corporate_controlled"] = True
        self.assertEqual(independent_groups(sources), 2)
        self.assertFalse(has_external_original(sources, sources))
        self.assertFalse(has_external_original([sources[1]], sources))

    def test_transitive_deduplication(self):
        sources = f.fixture()["sources"]
        sources[0]["sha256"] = sources[1]["sha256"]
        sources[1]["publisher_group"] = sources[2]["publisher_group"]
        self.assertEqual(independent_groups(sources), 1)

    def test_gap_merge_keeps_critical_and_all_hints(self):
        critical = make_gap("market", "missing_evidence", "Verify demand", query_hints=["q1"])
        warning = make_gap("market", "missing_evidence", "Verify demand", severity="warning", query_hints=["q2"])
        result = merge_gaps([critical, warning])
        self.assertEqual(result[0]["severity"], "critical")
        self.assertEqual(result[0]["query_hints"], ["q1", "q2"])


class LLMAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_common_llmclient_receives_strict_response_models(self):
        class Client:
            async def generate(self, *, model, system_prompt, payload, response_model):
                self.model = model
                self.prompt = system_prompt
                return response_model.model_validate(payload)
        client = Client()
        judge = LLMClientJudge(client=client, model="configured-judge")
        for schema, payload in (
                (CLAIM_JUDGE_SCHEMA, {"support_status": "supported", "reason": "Evidence supports the claim", "supported_evidence_ids": ["E-1"]}),
                (SUFFICIENCY_JUDGE_SCHEMA, {"semantic_pass": True, "reason": "Both questions answered", "gaps": []}),
                (REPORT_JUDGE_SCHEMA, {"criterion_scores": {c: 3 for c in CRITERIA}, "findings": []})):
            self.assertEqual(await judge(prompt="Separate judge prompt", payload=payload, schema=schema), payload)
        self.assertEqual(client.model, "configured-judge")
        self.assertEqual(client.prompt, "Separate judge prompt")

    async def test_llmclient_adapter_rejects_boolean_scores(self):
        class Client:
            async def generate(self, *, response_model, **kwargs):
                return response_model.model_validate({"criterion_scores": {c: True for c in CRITERIA}, "findings": []})
        judge = LLMClientJudge(client=Client(), model="configured-judge")
        with self.assertRaises(ValueError):
            await judge(prompt="Judge", payload={}, schema=REPORT_JUDGE_SCHEMA)


if __name__ == "__main__":
    unittest.main()
