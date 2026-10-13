"""Whole-branch review reproductions exercise real orchestration boundaries."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from policy_fixtures import NOW, Resolver, assessor, contracts, draft, full_fixture, new_run
from main.agents.supervisor.agent import Supervisor, validate_current_gate
from main.graph.supervisor_nodes import make_abort_node, make_publish_node, make_worker_node, WorkerFailure
from test_supervisor_graph_acceptance import ready_to_publish, publish_fixture


def short_deadline(state):
    state['started_at'] = datetime.now(timezone.utc)
    state['deadline_at'] = state['started_at'] + timedelta(seconds=0.05)


async def hang(*args, **kwargs):
    await asyncio.Event().wait()


def test_review_initial_config_load_obeys_run_deadline():
    async def scenario():
        store, state = await new_run({'tool_timeout_seconds': 1, 'run_deadline_seconds': 1})
        short_deadline(state)
        store.load = hang
        result = await asyncio.wait_for(Supervisor(store, Resolver(), hang).step(state), 0.3)
        assert (result['status'], result['terminal_reason']) == ('incomplete', 'BUDGET_EXHAUSTED')
    asyncio.run(scenario())


def test_review_writer_uses_gate_snapshot_when_resolver_changes():
    async def scenario():
        c = contracts()
        store, state = await new_run()
        results, verdicts, bundle, supports, _ = await full_fixture(store, state)
        class ChangingResolver(Resolver):
            calls = 0
            async def resolve(self, ids):
                self.calls += 1
                found = await super().resolve(ids)
                if self.calls >= 2:
                    data = found.model_dump(mode='json')
                    data['evidence'][0].update(quote='Changed unapproved content', quote_hash=None)
                    return c.EvidenceBundle.model_validate(data)
                return found
        supervisor = Supervisor(store, ChangingResolver(bundle), await assessor(verdicts), now=lambda: NOW)
        result = await supervisor.step(state)
        assert result['next_action'] == 'writer'
        request = c.WriterRequest.model_validate(await store.load(result['active_task'].payload_ref))
        assert request.evidence[0].quote == 'Confirmed conditions from original trial.'
        c.validate_claim_supports(claims=[cl for r in results.values() for cl in r.claims], evidence=request.evidence, supports=request.claim_supports)
    asyncio.run(scenario())


def test_review_writer_boundary_refuses_tampered_approved_evidence():
    async def scenario():
        c = contracts()
        store, state = await new_run()
        _, verdicts, bundle, _, _ = await full_fixture(store, state)
        state = await Supervisor(store, Resolver(bundle), await assessor(verdicts), now=lambda: NOW).step(state)
        task = state['active_task']
        payload = await store.load(task.payload_ref)
        payload['evidence'][0].update(quote='Changed unapproved content', quote_hash=None)
        payload['evidence'][0] = c.Evidence.model_validate(payload['evidence'][0]).model_dump(mode='json')
        # Updating both task hash and artifact ref cannot turn old gate approval into new approval.
        ref = await store.save(payload, 'tampered-request.json')
        state['active_task'] = task.model_copy(update={'payload_ref': ref, 'input_hash': c.canonical_hash(payload)})
        invoked = False
        async def writer(request):
            nonlocal invoked
            invoked = True
            return draft(state)
        result = await make_worker_node('writer', writer, store=store, now=lambda: NOW)(state)
        assert not invoked
        assert result['pending_result'].error.code == 'ARTIFACT_INTEGRITY_ERROR'
    asyncio.run(scenario())


def test_review_company_scope_eligibility_claim_reaches_evaluator():
    async def scenario():
        c = contracts()
        store, state = await new_run()
        _, verdicts, bundle, _, eligibility = await full_fixture(store, state)
        supervisor = Supervisor(store, Resolver(bundle), await assessor(verdicts), now=lambda: NOW)
        state = await supervisor.step(state)
        claim = eligibility.claims[0]
        item = next(e for e in bundle.evidence if e.evidence_id == claim.evidence_ids[0])
        report = c.DraftReport(revision=state['report_revision'], evidence_epoch=state['evidence_epoch'], sections=[dict(section_id=s, title=s.value, paragraphs=[dict(paragraph_id='target', text=claim.text, kind='fact', claim_ids=[claim.claim_id], evidence_ids=claim.evidence_ids)] if s == c.SectionId.COMPANY_SCOPE else []) for s in c.SECTION_ORDER], claim_ids=[claim.claim_id], citations=[dict(source_id=item.source_id, evidence_id=item.evidence_id, paragraph_id='target')])
        state['pending_result'] = c.ResultEnvelope(task_id=state['active_task'].task_id, outcome='success', result_ref=await store.save(report, 'target-report.json'))
        result = await supervisor.step(state)
        assert (result['status'], result['next_action']) == ('running', 'evaluator')
        request = c.EvaluatorRequest.model_validate(await store.load(result['active_task'].payload_ref))
        assert next(cl for cl in request.claims if cl.claim_id == 'target-conditions') == claim
    asyncio.run(scenario())


@pytest.mark.parametrize('attempts,expected', [(1, 'eligibility'), (3, 'abort')])
def test_review_invalid_dated_target_retries_eligibility(attempts, expected):
    async def scenario():
        c = contracts()
        store, state = await new_run()
        _, verdicts, bundle, _, eligibility = await full_fixture(store, state)
        future = c.Evidence.model_validate(bundle.evidence[0].model_dump(mode='json') | {'evidence_id': 'target-future', 'event_date': '2026-10-14'})
        bundle.evidence.append(future)
        claim = eligibility.claims[0].model_dump(mode='json') | {'evidence_ids': [future.evidence_id]}
        eligibility = c.EligibilityResult.model_validate(eligibility.model_dump(mode='json') | {'claims': [claim], 'result_hash': None})
        state['eligibility_ref'] = await store.save(eligibility, 'eligibility.json')
        state['eligibility_attempts'] = attempts
        result = await Supervisor(store, Resolver(bundle), await assessor(verdicts), now=lambda: NOW).step(state)
        assert result['next_action'] == expected
        assert result['step_count'] == state['step_count'] + 1
        if expected == 'eligibility':
            assert result['eligibility_attempts'] == 2
            request = c.EligibilityRequest.model_validate(await store.load(result['active_task'].payload_ref))
            assert request.gap_ids
            assert any('assessment date' in instruction for instruction in request.instructions)
        else:
            assert (result['status'], result['terminal_reason']) == ('incomplete', 'ELIGIBILITY_UNKNOWN')
    asyncio.run(scenario())


def test_review_permanent_publish_failure_and_diagnostics_are_failed():
    async def scenario():
        c = contracts()
        store, state, config = await ready_to_publish()
        async def auth_failure(request):
            raise WorkerFailure(c.ErrorSummary(code='CONFIG_OR_AUTH_ERROR', node='publish', retriable=False, message='Missing renderer configuration'))
        result = await make_publish_node(auth_failure, store=store, limits=config, now=lambda: NOW)(state)
        assert (result['status'], result['terminal_reason']) == ('failed', 'CONFIG_OR_AUTH_ERROR')
        assert store.payloads['diagnostics.json']['status'] == 'failed'
    asyncio.run(scenario())


def test_review_publish_deadline_timeout_is_incomplete():
    async def scenario():
        store, state, config = await ready_to_publish()
        short_deadline(state)
        result = await make_publish_node(hang, store=store, limits=config)(state)
        assert (result['status'], result['terminal_reason']) == ('incomplete', 'BUDGET_EXHAUSTED')
        assert store.payloads['diagnostics.json']['terminal_reason'] == 'BUDGET_EXHAUSTED'
    asyncio.run(scenario())


@pytest.mark.parametrize('operation', ['save', 'decision_sink'])
def test_review_supervisor_external_writes_and_sink_obey_deadline(operation):
    async def scenario():
        store, state = await new_run()
        short_deadline(state)
        if operation == 'save':
            store.save = hang
        result = await asyncio.wait_for(Supervisor(store, Resolver(), hang,
            decision_sink=hang if operation == 'decision_sink' else None).step(state), 0.3)
        assert (result['status'], result['terminal_reason']) == ('incomplete', 'BUDGET_EXHAUSTED')
    asyncio.run(scenario())


@pytest.mark.parametrize('operation', ['load', 'verify', 'find_receipt', 'save', 'save_receipt'])
def test_review_worker_storage_calls_obey_deadline(operation):
    async def scenario():
        store, state = await new_run()
        _, verdicts, bundle, _, _ = await full_fixture(store, state)
        state = await Supervisor(store, Resolver(bundle), await assessor(verdicts), now=lambda: NOW).step(state)
        setattr(store, operation, hang)
        short_deadline(state)
        async def writer(request):
            return draft(state)
        result = await asyncio.wait_for(make_worker_node('writer', writer, store=store)(state), 0.3)
        assert result['pending_result'].error.code == 'BUDGET_EXHAUSTED'
    asyncio.run(scenario())


@pytest.mark.parametrize('operation', ['load', 'verify', 'find_manifest', 'commit_manifest'])
def test_review_publish_storage_calls_obey_deadline(operation):
    async def scenario():
        store, state, config = await ready_to_publish()
        setattr(store, operation, hang)
        short_deadline(state)
        async def publish(request):
            return await publish_fixture(store, request)
        result = await asyncio.wait_for(make_publish_node(publish, store=store, limits=config)(state), 0.3)
        assert (result['status'], result['terminal_reason']) == ('incomplete', 'BUDGET_EXHAUSTED')
        assert store.payloads['diagnostics.json']['terminal_reason'] == 'BUDGET_EXHAUSTED'
    asyncio.run(scenario())


def test_review_stalled_terminal_diagnostics_have_finite_tool_timeout():
    async def scenario():
        c = contracts()
        store, state, config = await ready_to_publish()
        config = config.model_copy(update={'tool_timeout_seconds': 1})
        real_save = store.save
        async def save(payload, key, **kwargs):
            if key == 'diagnostics.json':
                return await hang()
            return await real_save(payload, key, **kwargs)
        store.save = save
        async def failure(request):
            raise WorkerFailure(c.ErrorSummary(code='CONFIG_OR_AUTH_ERROR', node='publish', retriable=False, message='Missing renderer configuration'))
        result = await asyncio.wait_for(make_publish_node(failure, store=store, limits=config, now=lambda: NOW)(state), 1.3)
        assert (result['status'], result['terminal_reason']) == ('failed', 'CONFIG_OR_AUTH_ERROR')
        assert result['node_status']['abort'] == 'diagnostics_failed'
    asyncio.run(scenario())


def test_review_large_accepted_result_restores_bounded_checkpoint_and_external_details():
    async def scenario():
        from policy_fixtures import eligible
        from main.graph.supervisor_state import validate_state
        c = contracts()
        store, state = await new_run()
        await eligible(store, state)
        supervisor = Supervisor(store, Resolver(), hang, now=lambda: NOW)
        state = await supervisor.step(state)
        task = state['active_task']
        result = c.PerspectiveResult(task_id=task.task_id, perspective=task.perspective,
            attempt=task.logical_attempt, claims=[], evidence_ids=[], answered_questions={},
            gaps=[c.Gap(gap_id='gap-'+str(n).zfill(4)+'-'+'x'*60, perspective=task.perspective,
                kind='missing_evidence', question='Need source', severity='critical') for n in range(600)],
            limitations=['No evidence'], retrieval_receipts=[])
        ref = await store.save(result, 'oversized-result.json')
        state['pending_result'] = c.ResultEnvelope(task_id=task.task_id, outcome='empty', result_ref=ref)
        previous = state['perspectives'][task.perspective]
        bounded = await supervisor.step(state)
        assert (bounded['status'], bounded['terminal_reason']) == ('failed', 'STATE_TOO_LARGE')
        assert validate_state(bounded)['perspectives'][task.perspective] == previous
        assert bounded['final_manifest_ref'] is None
        assert len(c.PerspectiveResult.model_validate(await store.load(ref)).gaps) == 600
        diagnostic = await make_abort_node(store=store, now=lambda: NOW)(bounded)
        assert diagnostic['node_status']['abort'] == 'diagnostics_saved'
        assert store.payloads['diagnostics.json']['terminal_reason'] == 'STATE_TOO_LARGE'
    asyncio.run(scenario())


def test_review_initial_config_read_honors_constructor_tool_limit():
    async def scenario():
        store, state = await new_run({'tool_timeout_seconds': 1})
        config = contracts().RunConfig.model_validate(await store.load(state['config_ref']))
        store.load = hang
        result = await asyncio.wait_for(Supervisor(store, Resolver(), hang, limits=config, now=lambda: NOW).step(state), 1.3)
        assert result['status'] == 'failed'
        assert result['terminal_reason'] != 'BUDGET_EXHAUSTED'
    asyncio.run(scenario())


def test_review_provider_timeout_before_deadline_is_failed():
    async def scenario():
        store, state = await new_run()
        async def provider_timeout(ref):
            raise TimeoutError('provider failed before the local deadline')
        store.load = provider_timeout
        result = await Supervisor(store, Resolver(), hang, now=lambda: NOW).step(state)
        assert result['status'] == 'failed'
        assert result['terminal_reason'] != 'BUDGET_EXHAUSTED'
    asyncio.run(scenario())


def test_review_initialization_storage_save_obeys_deadline():
    async def scenario():
        from main.graph.supervisor_workflow import initialize_run
        from main.agents.supervisor.agent import ServiceTimeout
        store, state = await new_run({'tool_timeout_seconds': 1, 'run_deadline_seconds': 1})
        c = contracts()
        config = c.RunConfig.model_validate(await store.load(state['config_ref']))
        run_input = c.RunInput.model_validate(await store.load(state['input_ref']))
        store.save = hang
        with pytest.raises(ServiceTimeout) as error:
            await asyncio.wait_for(initialize_run(run_input, config, store=store), 1.3)
        assert error.value.deadline_exhausted
    asyncio.run(scenario())


def test_review_timed_out_commit_replay_reuses_idempotent_manifest():
    async def scenario():
        store, state, config = await ready_to_publish()
        config = config.model_copy(update={'tool_timeout_seconds': 1})
        real_commit = store.commit_manifest
        async def commit_then_stall(manifest):
            await real_commit(manifest)
            await hang()
        store.commit_manifest = commit_then_stall
        async def publish(request):
            return await publish_fixture(store, request)
        first = await asyncio.wait_for(make_publish_node(publish, store=store, limits=config, now=lambda: NOW)(state), 1.3)
        assert first['status'] == 'failed'
        assert first['final_manifest_ref'] is None
        async def forbidden(request):
            raise AssertionError('Committed manifest key must skip renderer on replay')
        replay = await make_publish_node(forbidden, store=store, limits=config, now=lambda: NOW)(state)
        assert replay['status'] == 'completed'
        assert replay['final_manifest_ref'] is not None
    asyncio.run(scenario())


@pytest.mark.parametrize('conflict', [False, True])
def test_review_evaluator_target_claim_identity_deduplication(conflict):
    async def scenario():
        c = contracts()
        store, state = await new_run()
        results, verdicts, bundle, _, eligibility = await full_fixture(store, state)
        common = results[c.Perspective.MARKET].claims[0]
        target = common.model_copy(update={'text': 'A conflicting target fact'}) if conflict else common
        eligibility = c.EligibilityResult.model_validate(eligibility.model_dump(mode='json') | {'claims': [target.model_dump(mode='json')], 'result_hash': None})
        state['eligibility_ref'] = await store.save(eligibility, 'eligibility.json')
        supervisor = Supervisor(store, Resolver(bundle), await assessor(verdicts), now=lambda: NOW)
        state = await supervisor.step(state)
        if conflict:
            assert (state['status'], state['terminal_reason']) == ('failed', 'ARTIFACT_INTEGRITY_ERROR')
            return
        assert state['next_action'] == 'writer'
        state['pending_result'] = c.ResultEnvelope(task_id=state['active_task'].task_id, outcome='success', result_ref=await store.save(draft(state), 'draft.json'))
        state = await supervisor.step(state)
        assert state['next_action'] == 'evaluator'
        request = c.EvaluatorRequest.model_validate(await store.load(state['active_task'].payload_ref))
        assert len([cl for cl in request.claims if cl.claim_id == common.claim_id]) == 1
    asyncio.run(scenario())


def test_review_graph_bootstraps_initial_store_read_from_frozen_limits():
    async def scenario():
        from langgraph.checkpoint.memory import InMemorySaver
        from main.graph.supervisor_workflow import build_supervisor_graph
        store, state = await new_run({'tool_timeout_seconds': 1})
        config = contracts().RunConfig.model_validate(await store.load(state['config_ref']))
        supervisor = Supervisor(store, Resolver(), hang, now=lambda: NOW)
        store.load = hang
        workers = {node: hang for node in ('eligibility', 'writer', 'evaluator', *(p.value for p in contracts().PERSPECTIVES))}
        graph = build_supervisor_graph(supervisor=supervisor, workers=workers, publish_worker=hang,
            store=store, run_config=config, checkpointer=InMemorySaver(), now=lambda: NOW)
        result = await asyncio.wait_for(graph.ainvoke(state, {'configurable': {'thread_id': state['thread_id']}}), 1.3)
        assert result['status'] == 'failed'
        assert store.payloads['diagnostics.json']['status'] == 'failed'
    asyncio.run(scenario())


def test_review_evaluator_boundary_refuses_changed_gate_evidence():
    async def scenario():
        c = contracts()
        store, state = await new_run()
        _, verdicts, bundle, _, _ = await full_fixture(store, state)
        supervisor = Supervisor(store, Resolver(bundle), await assessor(verdicts), now=lambda: NOW)
        state = await supervisor.step(state)
        state['pending_result'] = c.ResultEnvelope(task_id=state['active_task'].task_id, outcome='success', result_ref=await store.save(draft(state), 'draft.json'))
        state = await supervisor.step(state)
        task = state['active_task']
        payload = await store.load(task.payload_ref)
        payload['evidence'][0].update(quote='Changed unapproved content', quote_hash=None)
        payload['evidence'][0] = c.Evidence.model_validate(payload['evidence'][0]).model_dump(mode='json')
        ref = await store.save(payload, 'tampered-evaluator-request.json')
        state['active_task'] = task.model_copy(update={'payload_ref': ref, 'input_hash': c.canonical_hash(payload)})
        invoked = False
        async def evaluate(request):
            from policy_fixtures import evaluation
            nonlocal invoked
            invoked = True
            return evaluation(request.report)
        result = await make_worker_node('evaluator', evaluate, store=store, now=lambda: NOW)(state)
        assert not invoked
        assert result['pending_result'].error.code == 'ARTIFACT_INTEGRITY_ERROR'
    asyncio.run(scenario())


def test_review_storage_provider_failure_returns_failed_state():
    async def scenario():
        store, state = await new_run()
        async def failure(ref):
            raise RuntimeError('storage provider failed')
        store.load = failure
        result = await Supervisor(store, Resolver(), hang, now=lambda: NOW).step(state)
        assert (result['status'], result['terminal_reason']) == ('failed', 'ARTIFACT_INTEGRITY_ERROR')
    asyncio.run(scenario())


def test_review_publish_remaining_budget_timeout_with_injected_clock():
    async def scenario():
        store, state, config = await ready_to_publish()
        state['deadline_at'] = NOW + timedelta(seconds=0.05)
        result = await make_publish_node(hang, store=store, limits=config, now=lambda: NOW)(state)
        assert (result['status'], result['terminal_reason']) == ('incomplete', 'BUDGET_EXHAUSTED')
    asyncio.run(scenario())


def test_review_worker_remaining_budget_timeout_with_injected_clock():
    async def scenario():
        store, state = await new_run()
        _, verdicts, bundle, _, _ = await full_fixture(store, state)
        state = await Supervisor(store, Resolver(bundle), await assessor(verdicts), now=lambda: NOW).step(state)
        state['deadline_at'] = NOW + timedelta(seconds=0.05)
        result = await make_worker_node('writer', hang, store=store, now=lambda: NOW)(state)
        assert result['pending_result'].error.code == 'BUDGET_EXHAUSTED'
    asyncio.run(scenario())
