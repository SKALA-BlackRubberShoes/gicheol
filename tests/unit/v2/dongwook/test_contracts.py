"""Boundary tests catch invalid persisted payloads, forged hashes, and unsafe refs."""
import hashlib
import importlib
import json
import pytest
from pydantic import ValidationError


def api():
    try:
        return importlib.import_module("main.agents.supervisor.contracts")
    except ModuleNotFoundError:
        pytest.fail("V2 shared contracts are not implemented")


def ref(path="evidence/a.json"):
    return dict(relative_path=path, sha256="a" * 64, media_type="application/json", schema_version="1.0")


def claim(**changes):
    payload = dict(claim_id="c1", perspective="market", text="Confirmed demand", kind="fact", evidence_ids=["e1"], limitations=["Limited to one trial"])
    return payload | changes


def perspective(**changes):
    payload = dict(task_id="run:market:1", perspective="market", attempt=1, claims=[claim()], evidence_ids=["e1"], answered_questions={"market.demand": ["c1"]}, gaps=[], limitations=["Trial only"], retrieval_receipts=[])
    return payload | changes


def report(**changes):
    sections = [dict(section_id=s, title=s, paragraphs=[]) for s in ("SUMMARY", "company_scope", "technology_maturity", "market", "stakeholders", "domain_application", "limitations", "REFERENCE")]
    sections[3]["paragraphs"] = [dict(paragraph_id="p1", text="Confirmed demand.", kind="fact", claim_ids=["c1"], evidence_ids=["e1"])]
    return dict(revision=1, evidence_epoch=1, sections=sections, claim_ids=["c1"], citations=[dict(source_id="s1", evidence_id="e1", paragraph_id="p1")], document_ref=None) | changes


def test_canonical_hash_is_independent_of_mapping_order_and_keeps_unicode():
    c = api()
    expected = '{"a":[1,true],"z":"한글"}'
    assert c.canonical_json({"z": "한글", "a": [1, True]}) == expected
    assert c.canonical_hash({"z": "한글", "a": [1, True]}) == hashlib.sha256(expected.encode()).hexdigest()


@pytest.mark.parametrize("path", ["/tmp/a", "../a", "a/../b", "a//b", "a\\b", "C:/a", "a/./b", "a/", "", "https://example.com/a", "a\x00b"])
def test_artifact_ref_rejects_paths_that_escape_or_ambiguously_resolve(path):
    with pytest.raises(ValidationError):
        api().ArtifactRef(**ref(path))


@pytest.mark.parametrize("digest", ["a" * 63, "G" * 64, "A" * 64, ""])
def test_artifact_ref_rejects_noncanonical_sha256(digest):
    with pytest.raises(ValidationError):
        api().ArtifactRef(**(ref() | {"sha256": digest}))


def test_contracts_reject_unknown_fields_and_integer_coercion():
    c = api()
    with pytest.raises(ValidationError):
        c.ArtifactRef(**(ref() | {"surprise": True}))
    with pytest.raises(ValidationError):
        c.PerspectiveSlot(attempt="1")
    with pytest.raises(ValidationError):
        c.RunConfig(max_quality_repairs=True)


@pytest.mark.parametrize("changes", [{"company_name": " "}, {"official_url": "file:///tmp/a"}, {"domain": "Finance"}, {"as_of_date": "2099-01-01"}])
def test_run_input_rejects_invalid_target_or_future_date(changes):
    payload = dict(company_name="Acme", official_url="https://example.com", domain="Physical AI / Robotics", as_of_date="2026-01-01", corpus_manifest=[])
    with pytest.raises(ValidationError):
        api().RunInput(**(payload | changes))


def test_source_requires_origin_and_content_integrity_ref():
    c = api()
    payload = dict(source_id="s1", title="Trial", publisher="Institute", publisher_group="Institute", source_type="institution", published_at=None, retrieved_at="2026-01-01T00:00:00Z", corporate_controlled=False, sha256="a" * 64, content_ref=ref())
    with pytest.raises(ValidationError):
        c.Source(**payload)
    assert c.Source(**(payload | {"canonical_url": "https://example.com/trial"})).published_at is None
    with pytest.raises(ValidationError):
        c.Source(**(payload | {"canonical_url": "https://example.com/trial", "sha256": "b" * 64}))


def test_quote_hash_must_match_actual_quote():
    c = api()
    payload = dict(evidence_id="e1", source_id="s1", company_id="acme", chunk_id="page-2", page_number=2, quote="Actual quote", event_date=None)
    evidence = c.Evidence(**payload)
    assert evidence.quote_hash == hashlib.sha256(b"Actual quote").hexdigest()
    with pytest.raises(ValidationError):
        c.Evidence(**(payload | {"quote_hash": "a" * 64}))


@pytest.mark.parametrize("changes", [{"kind": "fact", "evidence_ids": []}, {"kind": "inference", "evidence_ids": []}, {"evidence_ids": ["e1", "e1"]}, {"text": ""}])
def test_claim_rejects_unsupported_or_ambiguous_evidence(changes):
    with pytest.raises(ValidationError):
        api().Claim(**claim(**changes))


@pytest.mark.parametrize("changes", [{"answered_questions": {"invented.question": ["c1"]}}, {"answered_questions": {"tech.principle": ["c1"]}}, {"answered_questions": {"market.demand": ["missing"]}}, {"evidence_ids": []}, {"claims": [claim(perspective="stakeholders")]}, {"claims": [claim(), claim()]}, {"result_hash": "a" * 64}])
def test_perspective_result_rejects_broken_question_claim_or_evidence_links(changes):
    with pytest.raises(ValidationError):
        api().PerspectiveResult(**perspective(**changes))


def test_perspective_hash_preserves_semantic_changes_and_excludes_receipt_time():
    c = api()
    receipt = dict(query_id="q1", query_text="demand", queries_hash="a" * 64, provider="pdf", corpus_version="v1", index_version="v1", filters={"company_id": "acme", "domain": "Physical AI / Robotics"}, ranked_hits=[dict(source_id="s1", chunk_id="pdf-1", rank=1, score=0.8)], outcome="success", ts="2026-01-01T00:00:00Z")
    one = c.PerspectiveResult(**perspective(retrieval_receipts=[receipt]))
    two = c.PerspectiveResult(**perspective(retrieval_receipts=[receipt | {"ts": "2026-01-02T00:00:00Z"}]))
    assert one.result_hash == two.result_hash
    assert one.result_hash != c.PerspectiveResult(**perspective(claims=[claim(text="Changed demand")], retrieval_receipts=[receipt])).result_hash


@pytest.mark.parametrize("flags,verdict", [((True, True, True), "eligible"), ((True, "unknown", True), "unknown"), ((False, "unknown", True), "ineligible")])
def test_eligibility_does_not_promote_unknown_conditions(flags, verdict):
    c = api()
    payload = dict(task_id="eligibility:1", unlisted=flags[0], funding_stage=flags[1], exit_status=flags[2], verdict=verdict, claims=[], gaps=[])
    assert c.EligibilityResult(**payload).verdict == verdict
    with pytest.raises(ValidationError):
        c.EligibilityResult(**(payload | {"verdict": "eligible" if verdict != "eligible" else "unknown"}))


def test_draft_hash_has_exact_sdd_inputs_and_ignores_revision_and_document_ref():
    c = api()
    payload = report()
    expected = hashlib.sha256(json.dumps({k: payload[k] for k in ("sections", "claim_ids", "citations", "evidence_epoch")}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    draft = c.DraftReport(**payload)
    assert draft.content_hash == expected
    assert c.DraftReport(**(payload | {"revision": 2, "document_ref": ref("report.md")})).content_hash == expected
    assert c.DraftReport(**(payload | {"evidence_epoch": 2})).content_hash != expected


@pytest.mark.parametrize("changes", [{"content_hash": "a" * 64}, {"claim_ids": []}, {"citations": [dict(source_id="s1", evidence_id="missing", paragraph_id="p1")]}, {"sections": report()["sections"][::-1]}])
def test_draft_rejects_forged_hash_dangling_citation_and_wrong_section_order(changes):
    with pytest.raises(ValidationError):
        api().DraftReport(**report(**changes))


@pytest.mark.parametrize("scores,checks,findings", [({"groundedness": 2, "neutrality": 4, "bias_control": 4, "perspective_coverage": 4}, {"citations": True}, []), ({"groundedness": 3, "neutrality": 3, "bias_control": 3, "perspective_coverage": 3}, {"citations": False}, []), ({"groundedness": 3, "neutrality": 3, "bias_control": 3, "perspective_coverage": 3}, {"citations": True}, [dict(criterion="neutrality", severity="critical", section_id="market", claim_id=None, evidence_ids=[], reason="Recommendation", repair_kind="rewrite", target_perspective=None)])])
def test_quality_pass_cannot_hide_low_score_failed_check_or_critical_finding(scores, checks, findings):
    with pytest.raises(ValidationError):
        api().EvalVerdict(report_revision=1, report_hash="a" * 64, evidence_epoch=1, structural_checks=checks, criterion_scores=scores, findings=findings, overall_pass=True, repair_kind="none", rubric_version="1", judge_model="judge", prompt_hash="a" * 64)


def test_repair_plan_is_immutable_and_preserves_all_research_gaps():
    c = api()
    findings = [dict(criterion="groundedness", severity="critical", section_id=p, claim_id=None, evidence_ids=[], reason="Evidence missing", repair_kind="research", target_perspective=p) for p in ("market", "domain_application")]
    plan = c.RepairPlan(cycle_id="cycle-1", failed_report_revision=1, findings=findings, gap_ids=["gap-market", "gap-domain"], research_targets=["market", "domain_application"], rewrite_instructions=[])
    assert set(plan.research_targets) == {"market", "domain_application"}
    with pytest.raises(ValidationError):
        plan.cycle_id = "cycle-2"
    with pytest.raises(ValidationError):
        c.RepairPlan(**(plan.model_dump() | {"research_targets": ["market"]}))


@pytest.mark.parametrize("outcome,result_ref,error", [("success", None, None), ("permanent_error", ref(), None), ("success", ref(), dict(code="MODEL_OUTPUT_INVALID", node="market", retriable=False, message="Invalid"))])
def test_result_envelope_cannot_express_success_without_artifact_or_hide_error(outcome, result_ref, error):
    with pytest.raises(ValidationError):
        api().ResultEnvelope(task_id="task-1", outcome=outcome, result_ref=result_ref, error=error)


def test_task_envelope_requires_perspective_to_match_registered_worker():
    c = api()
    payload = dict(task_id="task-1", node="market", perspective="market", logical_attempt=1, report_revision=None, evidence_epoch=0, payload_ref=ref(), input_hash="a" * 64, dispatched_at="2026-01-01T00:00:00Z")
    assert c.TaskEnvelope(**payload).node == "market"
    with pytest.raises(ValidationError):
        c.TaskEnvelope(**(payload | {"perspective": "stakeholders"}))
    with pytest.raises(ValidationError):
        c.TaskEnvelope(**(payload | {"node": "publish"}))


def test_evidence_bundle_rejects_foreign_company_and_unknown_source():
    c = api()
    source = c.Source(source_id="s1", canonical_url="https://example.com", title="Trial", publisher="Institute", publisher_group="Institute", source_type="institution", retrieved_at="2026-01-01T00:00:00Z", corporate_controlled=False, sha256="a" * 64, content_ref=ref())
    evidence = c.Evidence(evidence_id="e1", source_id="s1", company_id="other", chunk_id="p1", page_number=1, quote="quote")
    with pytest.raises(ValueError):
        c.validate_evidence_bundle(company_id="acme", sources=[source], evidence=[evidence], claims=[c.Claim(**claim())])
    with pytest.raises(ValueError):
        c.validate_evidence_bundle(company_id="other", sources=[], evidence=[evidence], claims=[c.Claim(**claim())])


def test_evidence_bundle_requires_actual_pdf_page_number():
    c = api()
    source = c.Source(source_id="s1", original_file_id="trial.pdf", title="Trial", publisher=None, publisher_group="Institute", source_type="institution", retrieved_at="2026-01-01T00:00:00Z", corporate_controlled=False, sha256="a" * 64, content_ref=ref())
    evidence = c.Evidence(evidence_id="e1", source_id="s1", company_id="acme", chunk_id="pdf-1", quote="quote")
    with pytest.raises(ValueError):
        c.validate_evidence_bundle(company_id="acme", sources=[source], evidence=[evidence], claims=[c.Claim(**claim())])


def test_claim_support_rejects_stale_claim_and_evidence_hashes():
    c = api()
    evidence = c.Evidence(evidence_id="e1", source_id="s1", company_id="acme", chunk_id="p1", page_number=1, quote="quote")
    current_claim = c.Claim(**claim())
    # Independently serialized DTOs form the verifier binding, never worker status.
    claim_hash = hashlib.sha256(json.dumps(claim(), sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    evidence_payload = dict(evidence_id="e1", source_id="s1", company_id="acme", chunk_id="p1", page_number=1, quote="quote", quote_hash=hashlib.sha256(b"quote").hexdigest(), event_date=None)
    evidence_hash = hashlib.sha256(json.dumps(evidence_payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    support = c.ClaimSupport(claim_id="c1", claim_hash=claim_hash, evidence_hashes={"e1": evidence_hash}, support_status="supported", reason="Direct excerpt", verifier_version="1.0")
    c.validate_claim_supports(claims=[current_claim], evidence=[evidence], supports=[support])
    for changed in ({"claim_hash": "a" * 64}, {"evidence_hashes": {"e1": "b" * 64}}):
        with pytest.raises(ValueError):
            c.validate_claim_supports(claims=[current_claim], evidence=[evidence], supports=[c.ClaimSupport(**(support.model_dump() | changed))])


def test_evaluator_request_rejects_source_id_that_does_not_own_cited_evidence():
    c = api()
    source = c.Source(source_id="s1", canonical_url="https://example.com", title="Trial", publisher=None, publisher_group="Institute", source_type="institution", retrieved_at="2026-01-01T00:00:00Z", corporate_controlled=False, sha256="a" * 64, content_ref=ref())
    evidence = c.Evidence(evidence_id="e1", source_id="s1", company_id="acme", chunk_id="p1", page_number=1, quote="quote")
    forged = c.DraftReport(**report(citations=[dict(source_id="wrong-source", evidence_id="e1", paragraph_id="p1")]))
    with pytest.raises(ValidationError):
        c.EvaluatorRequest(task_id="eval-1", report=forged, claims=[c.Claim(**claim())], evidence=[evidence], sources=[source], as_of_date="2026-01-01")


def test_sufficiency_request_rejects_perspective_key_result_mismatch():
    c = api()
    eligibility = c.EligibilityResult(task_id="eligibility-1", unlisted=True, funding_stage=True, exit_status=True, verdict="eligible", claims=[], gaps=[])
    with pytest.raises(ValidationError):
        c.SufficiencyRequest(eligibility=eligibility, perspective_results={"stakeholders": c.PerspectiveResult(**perspective())}, as_of_date="2026-01-01", evidence_epoch=1)


def approved_inputs():
    c = api()
    eligibility = c.EligibilityResult(task_id="eligibility-1", unlisted=True, funding_stage=True, exit_status=True, verdict="eligible", claims=[], gaps=[])
    results = {p: c.PerspectiveResult(task_id=f"{p.value}-1", perspective=p, attempt=1, claims=[], evidence_ids=[], answered_questions={}, gaps=[], limitations=[], retrieval_receipts=[]) for p in c.PERSPECTIVES}
    gate = c.GateCertificate(evidence_epoch=1, eligibility_hash=eligibility.result_hash, perspective_hashes={p: r.result_hash for p, r in results.items()}, rubric_version="1.0", passed=True, reason_ref=ref())
    return eligibility, results, gate


def test_writer_request_rejects_gate_from_prior_evidence_epoch():
    c = api()
    eligibility, results, gate = approved_inputs()
    with pytest.raises(ValidationError):
        c.WriterRequest(task_id="writer-1", company_id="acme", as_of_date="2026-01-01", report_revision=1, evidence_epoch=2, eligibility=eligibility, perspective_results=results, gate=gate)


def test_publish_request_rejects_past_revision_even_when_content_hash_matches():
    c = api()
    eligibility, results, gate = approved_inputs()
    draft = c.DraftReport(**report(revision=2))
    evaluation = c.EvalVerdict(report_revision=1, report_hash=draft.content_hash, evidence_epoch=1, structural_checks={"citations": True}, criterion_scores={"groundedness": 3, "neutrality": 3, "bias_control": 3, "perspective_coverage": 3}, findings=[], overall_pass=True, repair_kind="none", rubric_version="1.0", judge_model="judge", prompt_hash="a" * 64)
    with pytest.raises(ValidationError):
        c.PublishRequest(task_id="publish-2", run_id="run-1", report=draft, evaluation=evaluation, gate=gate, eligibility=eligibility, perspective_results=results)


def test_completed_manifest_requires_pdf_validation_and_actual_pdf_reference():
    c = api()
    payload = dict(run_id="run-1", status="completed", report_revision=1, report_hash="b" * 64, evidence_epoch=1, eligibility_hash="c" * 64, perspective_hashes={p: "a" * 64 for p in c.PERSPECTIVES}, gate_ref=ref("gate.json"), draft_ref=ref("draft.json"), evaluation_ref=ref("evaluation.json"), report_pdf_ref=ref("report.pdf") | {"media_type": "application/pdf"}, pdf_validation_pass=True, trace_id="trace-1", versions={"schema": "1.0"}, committed_at="2026-01-01T00:00:00Z")
    assert c.Manifest(**payload).status == "completed"
    with pytest.raises(ValidationError):
        c.Manifest(**(payload | {"pdf_validation_pass": False}))
    with pytest.raises(ValidationError):
        c.Manifest(**(payload | {"report_pdf_ref": ref("report.json")}))


def test_run_input_enforces_deduplicated_pdf_corpus_page_budget():
    c = api()
    doc = dict(document_id="d1", file_path="trial.pdf", original_url=None, publisher=None, published_at="unknown", page_count=200, sha256="a" * 64)
    payload = dict(company_name="Acme", official_url="https://example.com", domain="Physical AI / Robotics", as_of_date="2026-01-01", corpus_manifest=[doc])
    assert len(c.RunInput(**payload).corpus_manifest) == 1
    assert len(c.RunInput(**(payload | {"corpus_manifest": [doc, doc | {"document_id": "d2"}]})).corpus_manifest) == 2
    with pytest.raises(ValidationError):
        c.RunInput(**(payload | {"corpus_manifest": [doc | {"page_count": 201}]}))
    with pytest.raises(ValidationError):
        c.RunInput(**(payload | {"corpus_manifest": [doc, doc | {"document_id": "d2", "page_count": 1, "sha256": "b" * 64}]}))


def test_repair_plan_nested_findings_cannot_be_mutated():
    c = api()
    finding = dict(criterion="groundedness", severity="critical", section_id="market", claim_id=None, evidence_ids=["e1"], reason="Evidence missing", repair_kind="research", target_perspective="market")
    plan = c.RepairPlan(cycle_id="cycle-1", failed_report_revision=1, findings=[finding], gap_ids=["gap-market"], research_targets=["market"], rewrite_instructions=[])
    with pytest.raises(ValidationError):
        plan.findings[0].reason = "Lost failure"
    assert plan.findings[0].evidence_ids == ("e1",)


def test_canonical_json_rejects_nonfinite_numbers_in_hash_input():
    with pytest.raises(ValueError):
        api().canonical_json({"score": float("nan")})


@pytest.mark.parametrize("value", ["20260101", 1767225600, "2026-01-01T00:00:00Z"])
def test_run_input_requires_yyyy_mm_dd_instead_of_numeric_or_datetime_dates(value):
    with pytest.raises(ValidationError):
        api().RunInput(company_name="Acme", official_url="https://example.com", domain="Physical AI / Robotics", as_of_date=value, corpus_manifest=[])


def test_unknown_claim_cannot_be_approved_by_a_support_binding():
    c = api()
    evidence = c.Evidence(evidence_id="e1", source_id="s1", company_id="acme", chunk_id="p1", quote="quote")
    current = c.Claim(**claim(kind="unknown"))
    support = c.ClaimSupport(claim_id="c1", claim_hash=c.canonical_hash(current), evidence_hashes={"e1": c.canonical_hash(evidence)}, support_status="supported", reason="Wrongly promoted", verifier_version="1.0")
    with pytest.raises(ValueError):
        c.validate_claim_supports(claims=[current], evidence=[evidence], supports=[support])


def test_async_protocols_can_be_replaced_without_importing_runtime_providers():
    import inspect
    c = api()
    # The callable contract requires coroutines so adapters do not block the graph.
    for protocol, names in [(c.ArtifactStore, ("load", "verify", "save", "find_receipt", "save_receipt", "commit_manifest", "find_manifest")), (c.EvidenceResolver, ("resolve",)), (c.ResearchTools, ("search", "fetch", "retrieve")), (c.SearchProvider, ("search", "fetch")), (c.EmbeddingProvider, ("embed_documents", "embed_query")), (c.LLMClient, ("generate",))]:
        assert all(inspect.iscoroutinefunction(getattr(protocol, name)) for name in names)
