"""Real orchestration tests catch dispatch, replay, budget and repair regressions."""
import asyncio
from datetime import timedelta

import pytest

from policy_fixtures import NOW, Resolver, contracts, eligible, new_run, no_verdicts, supervisor_api, full_fixture, assessor, draft, evaluation


def run(coro):
    return asyncio.run(coro)


def test_initial_task_reserves_attempt_before_dispatch_and_replays_without_increment():
    async def scenario():
        supervisor_api()
        store, state = await new_run()
        supervisor = supervisor_api().Supervisor(store, Resolver(), no_verdicts, now=lambda: NOW)
        patch = await supervisor.step(state)
        assert patch["next_action"] == "eligibility"
        assert patch["eligibility_attempts"] == 1
        assert patch["active_task"].logical_attempt == 1
        assert patch["active_task"].payload_ref is not None
        assert patch["step_count"] == 1
        replay = await supervisor.step(patch)
        assert replay["active_task"] == patch["active_task"]
        assert replay["eligibility_attempts"] == 1
        assert replay["step_count"] == 1
        assert state["eligibility_attempts"] == 0
        assert state["active_task"] is None
    run(scenario())


@pytest.mark.parametrize("missing", ["market", "technology_maturity"])
def test_dispatch_follows_insufficient_slot_and_preserves_other_views(missing):
    async def scenario():
        supervisor_api()
        store, state = await new_run()
        await eligible(store, state)
        c = contracts()
        for p in state["perspectives"]:
            state["perspectives"][p] = c.PerspectiveSlot(status="insufficient" if p == missing else "sufficient", result_hash="a" * 64 if p != missing else None, latest_result_ref=await store.save({"seed": p}, key=f"seed-{p}.json") if p != missing else None, sufficiency_ref=await store.save({"seed": p}, key=f"suff-{p}.json") if p != missing else None)
        supervisor = supervisor_api().Supervisor(store, Resolver(), no_verdicts, now=lambda: NOW)
        patch = await supervisor.step(state)
        assert patch["next_action"] == missing
        assert patch["research_attempts"][missing] == 1
        assert patch["active_task"].perspective == missing
        assert all(patch["perspectives"][p].result_hash == "a" * 64 for p in patch["perspectives"] if p != missing)
        assert state["research_attempts"][missing] == 0
    run(scenario())


def test_repeated_empty_results_exhaust_only_the_requested_view():
    async def scenario():
        supervisor_api()
        store, state = await new_run({"max_research_attempts_per_view": 3})
        results, verdicts, bundle, supports, eligibility = await full_fixture(store, state)
        c = contracts()
        state["perspectives"]["market"] = state["perspectives"]["market"].model_copy(update={"status": "insufficient"})
        state["research_attempts"]["market"] = 0
        async def assess_empty(request):
            current = request.perspective_results[c.Perspective.MARKET]
            missing = c.Gap(gap_id="market-empty", perspective="market", kind="missing_evidence", question="Seek original independent evidence", severity="critical")
            verdicts[c.Perspective.MARKET] = c.SufficiencyVerdict(perspective="market", structural_pass=False, semantic_pass=False, gaps=[missing], evidence_ids=current.evidence_ids, claim_support_refs=[], reason="No usable claims", rubric_version="1.0")
            return list(verdicts.values())
        supervisor = supervisor_api().Supervisor(store, Resolver(bundle), assess_empty, now=lambda: NOW)
        for attempt in range(1, 4):
            state = await supervisor.step(state)
            assert state["active_task"].logical_attempt == attempt
            task = state["active_task"]
            empty = c.PerspectiveResult(task_id=task.task_id, perspective="market", attempt=attempt, claims=[], evidence_ids=[], answered_questions={}, gaps=[], limitations=["No evidence found"], retrieval_receipts=[])
            state["pending_result"] = c.ResultEnvelope(task_id=task.task_id, outcome="empty", result_ref=await store.save(empty, key=f"empty-{attempt}.json"), error=None)
        state = await supervisor.step(state)
        assert state["status"] == "incomplete"
        assert state["terminal_reason"] == "INSUFFICIENT_EVIDENCE"
        assert state["next_action"] == "abort"
        assert state["research_attempts"]["market"] == 3
        assert state["perspectives"]["market"].gap_ids
        assert state["evidence_gate_pass"] is False
        assert state["evidence_epoch"] == 3
    run(scenario())


def test_deadline_aborts_without_dispatching_work():
    async def scenario():
        supervisor_api()
        store, state = await new_run()
        expired_now = state["deadline_at"] + timedelta(seconds=1)
        patch = await supervisor_api().Supervisor(store, Resolver(), no_verdicts, now=lambda: expired_now).step(state)
        assert patch["status"] == "incomplete"
        assert patch["terminal_reason"] == "BUDGET_EXHAUSTED"
        assert patch["next_action"] == "abort"
        assert patch["active_task"] is None
        assert patch["eligibility_attempts"] == 0
    run(scenario())


def test_stale_result_is_integrity_failure_and_never_accepted():
    async def scenario():
        supervisor_api()
        store, state = await new_run()
        c = contracts()
        supervisor = supervisor_api().Supervisor(store, Resolver(), no_verdicts, now=lambda: NOW)
        state = await supervisor.step(state)
        state["pending_result"] = c.ResultEnvelope(task_id="stale", outcome="empty", result_ref=await store.save({"outcome": "empty"}, key="empty.json"), error=None)
        patch = await supervisor.step(state)
        assert patch["status"] == "failed"
        assert patch["terminal_reason"] == "ARTIFACT_INTEGRITY_ERROR"
        assert "stale" not in patch["accepted_task_ids"]
    run(scenario())


def test_fresh_four_view_gate_drives_writer_evaluator_publish_with_latest_binding():
    async def scenario():
        store, state = await new_run()
        results, verdicts, bundle, supports, eligibility = await full_fixture(store, state)
        supervisor = supervisor_api().Supervisor(store, Resolver(bundle), await assessor(verdicts), now=lambda: NOW)
        state = await supervisor.step(state)
        assert state["next_action"] == "writer"
        assert state["report_revision"] == 1
        assert state["evidence_gate_pass"]
        assert await supervisor_api().validate_current_gate(state, store, now=lambda: NOW)
        request = contracts().WriterRequest.model_validate(await store.load(state["active_task"].payload_ref))
        assert request.report_revision == 1
        assert len(request.claim_supports) == 8
        report = draft(state)
        state["pending_result"] = contracts().ResultEnvelope(task_id=state["active_task"].task_id, outcome="success", result_ref=await store.save(report, key="draft.json"))
        state = await supervisor.step(state)
        assert state["next_action"] == "evaluator"
        ev = evaluation(report)
        state["pending_result"] = contracts().ResultEnvelope(task_id=state["active_task"].task_id, outcome="success", result_ref=await store.save(ev, key="eval.json"))
        state = await supervisor.step(state)
        assert state["next_action"] == "publish"
        assert state["active_task"] is None
        assert state["status"] == "running"
    run(scenario())


def test_last_allowed_research_can_pass_gate_without_an_extra_attempt():
    async def scenario():
        store, state = await new_run()
        results, verdicts, bundle, supports, eligibility = await full_fixture(store, state)
        c = contracts()
        market = state["perspectives"][c.Perspective.MARKET]
        state["research_attempts"]["market"] = 2
        state["perspectives"]["market"] = market.model_copy(update={"status": "insufficient", "attempt": 2})
        # A failed semantic verdict makes the third attempt necessary.
        old = verdicts[c.Perspective.MARKET]
        failed = c.SufficiencyVerdict(perspective="market", structural_pass=True, semantic_pass=False, gaps=[], evidence_ids=old.evidence_ids, claim_support_refs=old.claim_support_refs, reason="Need additional validation", rubric_version="1.0")
        verdicts[c.Perspective.MARKET] = failed
        supervisor = supervisor_api().Supervisor(store, Resolver(bundle), await assessor(verdicts), now=lambda: NOW)
        state = await supervisor.step(state)
        assert state["next_action"] == "market"
        task = state["active_task"]
        assert task.logical_attempt == 3
        fresh = c.PerspectiveResult(**(results[c.Perspective.MARKET].model_dump(mode="json") | {"task_id": task.task_id, "attempt": 3, "result_hash": None}))
        verdicts[c.Perspective.MARKET] = old
        state["pending_result"] = c.ResultEnvelope(task_id=task.task_id, outcome="success", result_ref=await store.save(fresh, key="market-third.json"))
        state = await supervisor.step(state)
        assert state["next_action"] == "writer"
        assert state["research_attempts"]["market"] == 3
        assert state["evidence_epoch"] == 1
        assert state["evidence_gate_pass"]
    run(scenario())


def test_transient_error_reuses_reserved_task_and_attempt_without_accepting_failure():
    async def scenario():
        store, state = await new_run()
        c = contracts()
        supervisor = supervisor_api().Supervisor(store, Resolver(), no_verdicts, now=lambda: NOW)
        state = await supervisor.step(state)
        task = state["active_task"]
        state["pending_result"] = c.ResultEnvelope(task_id=task.task_id, outcome="transient_error", error=c.ErrorSummary(code="MODEL_OUTPUT_INVALID", node="eligibility", retriable=True, message="Provider temporary failure"))
        retry = await supervisor.step(state)
        assert retry["active_task"] == task
        assert retry["eligibility_attempts"] == 1
        assert retry["step_count"] == 1
        assert retry["transient_retries"] == 1
        assert retry["accepted_task_ids"] == []
        assert retry["pending_result"] is None
    run(scenario())


def test_two_research_findings_survive_eval_invalidation_in_one_repair_cycle():
    async def scenario():
        store, state = await new_run()
        results, verdicts, bundle, supports, eligibility = await full_fixture(store, state)
        c = contracts()
        supervisor = supervisor_api().Supervisor(store, Resolver(bundle), await assessor(verdicts), now=lambda: NOW)
        state = await supervisor.step(state)
        report = draft(state)
        state["pending_result"] = c.ResultEnvelope(task_id=state["active_task"].task_id, outcome="success", result_ref=await store.save(report, key="draft.json"))
        state = await supervisor.step(state)
        findings = [c.EvalFinding(criterion="groundedness", severity="critical", section_id=p, claim_id=None, evidence_ids=[], reason=f"Verify {p} original evidence", repair_kind="research", target_perspective=p) for p in ("market", "domain_application")]
        ev = evaluation(report, passing=False, findings=findings)
        state["pending_result"] = c.ResultEnvelope(task_id=state["active_task"].task_id, outcome="success", result_ref=await store.save(ev, key="eval-fail.json"))
        state = await supervisor.step(state)
        assert state["next_action"] == "domain_application"
        assert state["repair_cycles"] == 1
        cycle_id = state["active_repair_cycle_id"]
        plan_ref = state["repair_plan_ref"]
        plan = c.RepairPlan.model_validate(await store.load(plan_ref))
        assert set(plan.research_targets) == {"market", "domain_application"}
        assert len(plan.gap_ids) == 2
        market_gaps = list(state["perspectives"]["market"].gap_ids)
        for p in ("domain_application", "market"):
            task = state["active_task"]
            assert task.perspective == p
            request = c.ResearchRequest.model_validate(await store.load(task.payload_ref))
            assert request.gap_ids
            assert any(f"Verify {p}" in line for line in request.instructions)
            fresh = c.PerspectiveResult(**(results[c.Perspective(p)].model_dump(mode="json") | {"task_id": task.task_id, "attempt": task.logical_attempt, "result_hash": None}))
            state["pending_result"] = c.ResultEnvelope(task_id=task.task_id, outcome="success", result_ref=await store.save(fresh, key=f"repair-{p}.json"))
            state = await supervisor.step(state)
            assert state["repair_cycles"] == 1
            assert state["active_repair_cycle_id"] == cycle_id
            assert state["repair_plan_ref"] == plan_ref
            if p == "domain_application":
                assert state["next_action"] == "market"
                assert state["eval_ref"] is None
                assert state["perspectives"]["market"].gap_ids == market_gaps
        assert state["next_action"] == "writer"
        assert state["report_revision"] == 2
        assert state["evidence_epoch"] == 2
        assert set(state["resolved_gap_ids"]) == set(plan.gap_ids)
    run(scenario())


def test_second_allowed_rewrite_can_publish_and_third_failure_exhausts_quality():
    async def scenario(passing):
        store, state = await new_run()
        results, verdicts, bundle, supports, eligibility = await full_fixture(store, state)
        c = contracts()
        supervisor = supervisor_api().Supervisor(store, Resolver(bundle), await assessor(verdicts), now=lambda: NOW)
        state = await supervisor.step(state)
        for revision in (1, 2, 3):
            assert state["next_action"] == "writer"
            assert state["report_revision"] == revision
            report = draft(state)
            state["pending_result"] = c.ResultEnvelope(task_id=state["active_task"].task_id, outcome="success", result_ref=await store.save(report, key=f"draft-{revision}.json"))
            state = await supervisor.step(state)
            assert state["next_action"] == "evaluator"
            findings = [c.EvalFinding(criterion="neutrality", severity="critical", section_id="market", claim_id=None, evidence_ids=[], reason="Remove investment recommendation", repair_kind="rewrite", target_perspective=None)]
            ev = evaluation(report, passing=passing and revision == 3, findings=[] if passing and revision == 3 else findings)
            state["pending_result"] = c.ResultEnvelope(task_id=state["active_task"].task_id, outcome="success", result_ref=await store.save(ev, key=f"eval-{revision}.json"))
            state = await supervisor.step(state)
        assert state["repair_cycles"] == 2
        assert state["research_attempts"] == {p: 1 for p in c.PERSPECTIVES}
        if passing:
            assert state["next_action"] == "publish"
            assert state["status"] == "running"
        else:
            assert state["next_action"] == "abort"
            assert state["terminal_reason"] == "QUALITY_EXHAUSTED"
            assert state["status"] == "incomplete"
            assert state["draft_report_ref"] is not None
    run(scenario(True))
    run(scenario(False))


def test_gate_guard_rechecks_support_and_source_artifact_integrity():
    async def scenario(missing):
        store, state = await new_run()
        results, verdicts, bundle, supports, eligibility = await full_fixture(store, state)
        supervisor = supervisor_api().Supervisor(store, Resolver(bundle), await assessor(verdicts), now=lambda: NOW)
        state = await supervisor.step(state)
        assert await supervisor_api().validate_current_gate(state, store, now=lambda: NOW)
        ref = verdicts[contracts().Perspective.MARKET].claim_support_refs[0] if missing == "support" else bundle.sources[0].content_ref
        del store.payloads[ref.relative_path]
        with pytest.raises((KeyError, ValueError)):
            await supervisor_api().validate_current_gate(state, store, now=lambda: NOW)
    run(scenario("support"))
    run(scenario("source"))


def test_duplicate_accepted_result_does_not_change_epoch_or_new_task():
    async def scenario():
        store, state = await new_run()
        c = contracts()
        supervisor = supervisor_api().Supervisor(store, Resolver(), no_verdicts, now=lambda: NOW)
        state = await supervisor.step(state)
        task = state["active_task"]
        result = c.EligibilityResult(task_id=task.task_id, unlisted=True, funding_stage=True, exit_status=True, verdict="eligible", claims=[], gaps=[])
        accepted = c.ResultEnvelope(task_id=task.task_id, outcome="success", result_ref=await store.save(result, key="eligibility-final.json"))
        state["pending_result"] = accepted
        state = await supervisor.step(state)
        new_task = state["active_task"]
        state["pending_result"] = accepted
        replay = await supervisor.step(state)
        assert replay["active_task"] == new_task
        assert replay["step_count"] == state["step_count"]
        assert replay["evidence_epoch"] == state["evidence_epoch"]
        assert replay["accepted_task_ids"] == [task.task_id]
    run(scenario())


def test_mutated_perspective_hash_invalidates_certificate_and_fails_integrity():
    async def scenario():
        store, state = await new_run()
        results, verdicts, bundle, supports, eligibility = await full_fixture(store, state)
        supervisor = supervisor_api().Supervisor(store, Resolver(bundle), await assessor(verdicts), now=lambda: NOW)
        state = await supervisor.step(state)
        state["active_task"] = None
        state["perspectives"]["market"] = state["perspectives"]["market"].model_copy(update={"result_hash": "a" * 64})
        assert not await supervisor_api().validate_current_gate(state, store, now=lambda: NOW)
        patch = await supervisor.step(state)
        assert patch["status"] == "failed"
        assert patch["terminal_reason"] == "ARTIFACT_INTEGRITY_ERROR"
        assert patch["next_action"] == "abort"
    run(scenario())


def test_raw_pdf_source_is_verified_without_loading_it_as_json():
    async def scenario():
        store, state = await new_run()
        results, verdicts, bundle, supports, eligibility = await full_fixture(store, state)
        pdf_ref = await store.save_blob(b"%PDF-1.7\noriginal-file", "sources/original.pdf", "application/pdf")
        bundle.sources[0] = bundle.sources[0].model_copy(update={"content_ref": pdf_ref, "sha256": pdf_ref.sha256})
        supervisor = supervisor_api().Supervisor(store, Resolver(bundle), await assessor(verdicts), now=lambda: NOW)
        state = await supervisor.step(state)
        assert state["next_action"] == "writer"
        assert await supervisor_api().validate_current_gate(state, store, now=lambda: NOW)
    run(scenario())


def test_sufficiency_timeout_never_approves_writer():
    async def scenario():
        store, state = await new_run({"llm_timeout_seconds": 1})
        results, verdicts, bundle, supports, eligibility = await full_fixture(store, state)
        async def slow_assessor(request):
            await asyncio.sleep(10)
            return list(verdicts.values())
        supervisor = supervisor_api().Supervisor(store, Resolver(bundle), slow_assessor, now=lambda: NOW)
        patch = await asyncio.wait_for(supervisor.step(state), timeout=2)
        assert patch["status"] == "failed"
        assert patch["terminal_reason"] == "MODEL_OUTPUT_INVALID"
        assert patch["next_action"] == "abort"
        assert patch["active_task"] is None
        assert not patch["evidence_gate_pass"]
    run(scenario())
