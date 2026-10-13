"""Policy must prioritize the actual missing work and reject stale approval."""
import importlib
from types import SimpleNamespace as N

import pytest
import asyncio
from datetime import date
from policy_fixtures import MemoryStore, full_fixture, new_run, contracts


def policy():
    try:
        return importlib.import_module("main.agents.supervisor.policy")
    except ModuleNotFoundError:
        pytest.fail("Supervisor policy is not implemented")


def slots(missing):
    return {p: N(status="insufficient" if p == missing else "sufficient", gap_ids=["g"] if p == missing else []) for p in ("technology_maturity", "market", "stakeholders", "domain_application")}


@pytest.mark.parametrize("missing", ["market", "technology_maturity"])
def test_selects_only_the_insufficient_perspective(missing):
    assert policy().choose_research_target(slots(missing), []) == missing


def test_missing_question_outranks_source_bias_and_critical_count_breaks_tie():
    views = slots("market")
    views["domain_application"].status = "insufficient"
    gaps = [N(perspective="market", kind="source_bias", severity="critical"), N(perspective="domain_application", kind="missing_question", severity="warning")]
    assert policy().choose_research_target(views, gaps) == "domain_application"
    gaps += [N(perspective="market", kind="missing_question", severity="critical")]
    assert policy().choose_research_target(views, gaps) == "market"


def test_pending_views_use_string_tie_break_not_fixed_node_order():
    views = {p: N(status="pending", gap_ids=[]) for p in slots("market")}
    assert policy().choose_research_target(views, []) == "domain_application"


def test_gate_currentness_requires_all_hashes_and_epoch():
    state = dict(eligibility_status="eligible", evidence_gate_pass=True, evidence_epoch=4, perspectives={p: N(status="sufficient", result_hash=p) for p in slots("market")})
    gate = N(passed=True, evidence_epoch=4, eligibility_hash="eligibility", perspective_hashes={p: p for p in slots("market")}, rubric_version="r1")
    assert policy().gate_matches(state, gate, "eligibility", "r1")
    state["perspectives"]["market"].result_hash = "changed"
    assert not policy().gate_matches(state, gate, "eligibility", "r1")
    state["perspectives"]["market"].result_hash = "market"
    gate.evidence_epoch = 3
    assert not policy().gate_matches(state, gate, "eligibility", "r1")


def test_report_and_eval_require_latest_revision_hash_and_epoch():
    state = dict(report_revision=2, evidence_epoch=5)
    report = N(revision=2, evidence_epoch=5, content_hash="new")
    evaluation = N(report_revision=2, report_hash="new", evidence_epoch=5)
    assert policy().report_matches(state, report)
    assert policy().evaluation_matches(state, report, evaluation)
    evaluation.report_hash = "old"
    assert not policy().evaluation_matches(state, report, evaluation)
    report.evidence_epoch = 4
    assert not policy().report_matches(state, report)


def test_structural_gate_requires_current_supports_questions_publishers_and_real_pdf():
    async def scenario():
        store, state = await new_run()
        results, verdicts, bundle, supports, eligibility = await full_fixture(store, state)
        assert policy().structural_gaps("acme", date(2026, 10, 13), results, verdicts, bundle, list(supports.values()), eligibility) == []
        c = contracts()
        market = results[c.Perspective.MARKET]
        # Semantic pass cannot make a missing fixed question disappear.
        results[c.Perspective.MARKET] = c.PerspectiveResult(**(market.model_dump(mode="json") | {"answered_questions": {"market.demand": [market.claims[0].claim_id]}, "result_hash": None}))
        gaps = policy().structural_gaps("acme", date(2026, 10, 13), results, verdicts, bundle, list(supports.values()), eligibility)
        assert any(g.perspective == "market" and g.kind == "missing_question" for g in gaps)
    asyncio.run(scenario())


@pytest.mark.parametrize("mutation,expected_kind", [("publisher", "source_bias"), ("corporate", "source_bias"), ("future", "missing_evidence"), ("pdf_receipt", "missing_evidence"), ("no_support", "missing_evidence")])
def test_semantic_pass_cannot_override_structural_gate(mutation, expected_kind):
    async def scenario():
        store, state = await new_run()
        results, verdicts, bundle, supports, eligibility = await full_fixture(store, state)
        c = contracts()
        if mutation == "publisher":
            bundle.sources = [s.model_copy(update={"publisher_group": "same-republished-group"}) for s in bundle.sources]
        elif mutation == "corporate":
            bundle.sources = [s.model_copy(update={"corporate_controlled": True}) for s in bundle.sources]
        elif mutation == "future":
            bundle.sources = [s.model_copy(update={"published_at": date(2026, 11, 1)}) for s in bundle.sources]
        elif mutation == "pdf_receipt":
            r = results[c.Perspective.TECHNOLOGY_MATURITY]
            results[r.perspective] = c.PerspectiveResult(**(r.model_dump(mode="json") | {"retrieval_receipts": [], "result_hash": None}))
        else:
            supports = {}
        gaps = policy().structural_gaps("acme", date(2026, 10, 13), results, verdicts, bundle, list(supports.values()), eligibility)
        assert any(g.kind == expected_kind for g in gaps)
    asyncio.run(scenario())


def test_changed_evidence_rejects_stale_verifier_binding_as_integrity_error():
    async def scenario():
        store, state = await new_run()
        results, verdicts, bundle, supports, eligibility = await full_fixture(store, state)
        c = contracts()
        bundle.evidence[0] = c.Evidence(**(bundle.evidence[0].model_dump(mode="json") | {"quote": "Changed quote", "quote_hash": None}))
        with pytest.raises(ValueError, match="changed evidence"):
            policy().structural_gaps("acme", date(2026, 10, 13), results, verdicts, bundle, list(supports.values()), eligibility)
    asyncio.run(scenario())
