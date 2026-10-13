"""AT19 completion reconciliation must preserve immutable committed artifacts."""
import asyncio
from datetime import timedelta

import pytest

from policy_fixtures import NOW, contracts
from main.graph.supervisor_nodes import make_publish_node
from test_supervisor_graph_acceptance import ready_to_publish, publish_fixture


async def committed_fixture():
    store, state, config = await ready_to_publish()
    async def publish(request):
        return await publish_fixture(store, request)
    completed = await make_publish_node(publish, store=store, limits=config, now=lambda: NOW)(state)
    assert completed['status'] == 'completed'
    return store, state, config, completed['final_manifest_ref']


def test_at19_committed_manifest_reconciles_after_absolute_deadline():
    async def scenario():
        store, state, config, ref = await committed_fixture()
        invocations = []
        async def forbidden(request):
            invocations.append('render')
            raise AssertionError('Committed result recovery cannot render')
        async def commit_forbidden(manifest):
            invocations.append('commit')
            raise AssertionError('Committed result recovery cannot commit')
        store.commit_manifest = commit_forbidden
        after_deadline = state['deadline_at'] + timedelta(seconds=1)
        result = await make_publish_node(forbidden, store=store, limits=config, now=lambda: after_deadline)(state)
        assert result['status'] == 'completed'
        assert result['final_manifest_ref'] == ref
        assert len(store.manifests) == 1
        assert invocations == []
    asyncio.run(scenario())


def test_at19_absent_manifest_after_deadline_cannot_start_publication():
    async def scenario():
        store, state, config = await ready_to_publish()
        invocations = []
        async def forbidden(value):
            invocations.append('new publication')
            raise AssertionError('Missing committed record cannot authorize late work')
        store.commit_manifest = forbidden
        after_deadline = state['deadline_at'] + timedelta(seconds=1)
        result = await make_publish_node(forbidden, store=store, limits=config, now=lambda: after_deadline)(state)
        assert (result['status'], result['terminal_reason']) == ('incomplete', 'BUDGET_EXHAUSTED')
        assert result['final_manifest_ref'] is None
        assert store.manifests == {}
        assert invocations == []
    asyncio.run(scenario())


@pytest.mark.parametrize('damage', ['missing_pdf', 'changed_manifest_hash', 'stale_evaluation', 'stale_gate', 'changed_source'])
def test_at19_recovery_rejects_damaged_or_stale_committed_artifacts(damage):
    async def scenario():
        c = contracts()
        store, state, config, ref = await committed_fixture()
        manifest = c.Manifest.model_validate(await store.load(ref))
        if damage == 'missing_pdf':
            del store.payloads[manifest.report_pdf_ref.relative_path]
        elif damage == 'changed_manifest_hash':
            changed = manifest.model_copy(update={'report_hash': 'b' * 64})
            store.manifests[state['run_id'], state['report_revision']] = await store.save(changed, 'changed-manifest.json')
        elif damage == 'stale_evaluation':
            current = c.EvalVerdict.model_validate(await store.load(state['eval_ref']))
            state['eval_ref'] = await store.save(current.model_copy(update={'report_hash': 'b' * 64}), 'stale-evaluation.json')
        elif damage == 'stale_gate':
            slot = state['perspectives']['market']
            state['perspectives']['market'] = slot.model_copy(update={'result_hash': 'b' * 64})
        else:
            gate = c.GateCertificate.model_validate(await store.load(state['gate_ref']))
            reason = await store.load(gate.reason_ref)
            source = c.EvidenceBundle.model_validate(reason['evidence_bundle']).sources[0]
            store.payloads[source.content_ref.relative_path] = {'text': 'changed after approval'}
        invoked = []
        async def forbidden(request):
            invoked.append('render')
            raise AssertionError('Recovery must not repair damaged committed artifacts')
        after_deadline = state['deadline_at'] + timedelta(seconds=1)
        result = await make_publish_node(forbidden, store=store, limits=config, now=lambda: after_deadline)(state)
        assert (result['status'], result['terminal_reason']) == ('failed', 'ARTIFACT_INTEGRITY_ERROR')
        assert result['final_manifest_ref'] is None
        assert store.payloads['diagnostics.json']['status'] == 'failed'
        assert invoked == []
    asyncio.run(scenario())


@pytest.mark.parametrize('operation', ['find_manifest', 'load', 'verify'])
def test_at19_stalled_completion_recovery_is_tool_bounded(operation):
    async def scenario():
        store, state, config, _ = await committed_fixture()
        config = config.model_copy(update={'tool_timeout_seconds': 1})
        async def hang(*args, **kwargs):
            await asyncio.Event().wait()
        setattr(store, operation, hang)
        after_deadline = state['deadline_at'] + timedelta(seconds=1)
        result = await asyncio.wait_for(make_publish_node(hang, store=store, limits=config, now=lambda: after_deadline)(state), 1.3)
        assert (result['status'], result['terminal_reason']) == ('failed', 'ARTIFACT_INTEGRITY_ERROR')
        assert result['final_manifest_ref'] is None
        assert store.payloads['diagnostics.json']['status'] == 'failed'
    asyncio.run(scenario())


@pytest.mark.parametrize('commit_owner', ['wrapper', 'worker'])
def test_at19_commit_crossing_deadline_finishes_read_only_validation(commit_owner):
    async def scenario():
        c = contracts()
        store, state, config = await ready_to_publish()
        clock = [NOW]
        original_commit = store.commit_manifest
        async def commit(manifest):
            ref = await original_commit(manifest)
            clock[0] = state['deadline_at'] + timedelta(seconds=1)
            return ref
        store.commit_manifest = commit
        async def publish(request):
            outcome = await publish_fixture(store, request)
            if commit_owner == 'worker':
                manifest = c.Manifest(run_id=state['run_id'], status='completed',
                    report_revision=request.report.revision, report_hash=request.report.content_hash,
                    evidence_epoch=request.report.evidence_epoch, eligibility_hash=request.eligibility.result_hash,
                    perspective_hashes={p: result.result_hash for p, result in request.perspective_results.items()},
                    gate_ref=state['gate_ref'], draft_ref=state['draft_report_ref'], evaluation_ref=state['eval_ref'],
                    report_pdf_ref=outcome.staging_pdf_ref, pdf_validation_pass=True,
                    trace_id=state['trace_id'], versions=state['versions'], committed_at=clock[0])
                ref = await store.commit_manifest(manifest)
                return outcome.model_copy(update={'final_manifest_ref': ref})
            return outcome
        result = await make_publish_node(publish, store=store, limits=config, now=lambda: clock[0])(state)
        assert result['status'] == 'completed'
        assert result['final_manifest_ref'] is not None
        assert len(store.manifests) == 1
        assert clock[0] > state['deadline_at']
    asyncio.run(scenario())


def test_at19_graph_pending_publish_checkpoint_reconciles_after_deadline():
    async def scenario():
        from langgraph.checkpoint.memory import InMemorySaver
        from main.graph.supervisor_workflow import build_supervisor_graph
        from policy_fixtures import draft, evaluation
        from test_supervisor_graph_acceptance import fixture
        store, state, supervisor, config = await fixture()
        clock = [NOW]
        supervisor.now = lambda: clock[0]
        rendered = []
        async def unused(request):
            raise AssertionError('Approved target and views cannot be re-researched')
        async def writer(request):
            return draft({'report_revision': request.report_revision, 'evidence_epoch': request.evidence_epoch})
        async def evaluator(request):
            return evaluation(request.report)
        async def publish(request):
            rendered.append(request.report.revision)
            return await publish_fixture(store, request)
        workers = {node: unused for node in ('eligibility', *(p.value for p in contracts().PERSPECTIVES))}
        workers.update(writer=writer, evaluator=evaluator)
        graph = build_supervisor_graph(supervisor=supervisor, workers=workers, publish_worker=publish,
            store=store, run_config=config, checkpointer=InMemorySaver(), now=lambda: clock[0])
        invocation = {'configurable': {'thread_id': state['thread_id']}}
        await graph.ainvoke(state, invocation, interrupt_before=['publish'])
        snapshot = await graph.aget_state(invocation)
        assert snapshot.next == ('publish',)
        # Durable commit happened, but its State/checkpoint update was lost.
        published = await make_publish_node(publish, store=store, limits=config, now=lambda: clock[0])(snapshot.values)
        assert published['status'] == 'completed'
        clock[0] = snapshot.values['deadline_at'] + timedelta(seconds=1)
        recovered = await graph.ainvoke(None, invocation)
        assert recovered['status'] == 'completed'
        assert recovered['final_manifest_ref'] == published['final_manifest_ref']
        assert rendered == [1]
        assert len(store.manifests) == 1
    asyncio.run(scenario())


def test_at19_staging_only_return_after_deadline_cannot_start_commit():
    async def scenario():
        store, state, config = await ready_to_publish()
        clock = [NOW]
        committed = []
        async def forbidden(manifest):
            committed.append(manifest)
            raise AssertionError('Staging output cannot authorize a post-deadline commit')
        store.commit_manifest = forbidden
        async def publish(request):
            outcome = await publish_fixture(store, request)
            clock[0] = state['deadline_at'] + timedelta(seconds=1)
            return outcome
        result = await make_publish_node(publish, store=store, limits=config, now=lambda: clock[0])(state)
        assert (result['status'], result['terminal_reason']) == ('incomplete', 'BUDGET_EXHAUSTED')
        assert result['final_manifest_ref'] is None
        assert store.manifests == {}
        assert committed == []
    asyncio.run(scenario())


def test_at19_failed_commit_crossing_deadline_is_not_completed():
    async def scenario():
        store, state, config = await ready_to_publish()
        clock = [NOW]
        async def fail_commit(manifest):
            clock[0] = state['deadline_at'] + timedelta(seconds=1)
            raise OSError('Commit failed without a durable manifest')
        store.commit_manifest = fail_commit
        async def publish(request):
            return await publish_fixture(store, request)
        result = await make_publish_node(publish, store=store, limits=config, now=lambda: clock[0])(state)
        assert (result['status'], result['terminal_reason']) == ('failed', 'ARTIFACT_INTEGRITY_ERROR')
        assert result['final_manifest_ref'] is None
        assert store.manifests == {}
        assert store.payloads['diagnostics.json']['status'] == 'failed'
    asyncio.run(scenario())
