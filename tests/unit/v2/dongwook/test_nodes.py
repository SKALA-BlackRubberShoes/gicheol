"""The node boundary exposes a narrow DTO and reuses persisted task receipts."""
import asyncio
from copy import deepcopy
from datetime import timedelta

from main.agents.supervisor.contracts import (
    ArtifactRef, PerspectiveResult, ResearchRequest, TaskEnvelope, canonical_hash,
)
from main.graph.supervisor_nodes import make_worker_node
from test_state import make_state, NOW


class Store:
    def __init__(self):
        self.items, self.receipts = {}, {}

    async def save(self, payload, *, key, media_type="application/json"):
        ref = ArtifactRef(relative_path=key, sha256=canonical_hash(payload),
                          media_type=media_type, schema_version="1.0")
        self.items[key] = deepcopy(payload)
        return ref

    async def load(self, ref):
        value = self.items[ref.relative_path]
        if canonical_hash(value) != ref.sha256:
            raise ValueError("artifact hash mismatch")
        return deepcopy(value)

    async def verify(self, ref):
        await self.load(ref)

    async def find_receipt(self, task_id, input_hash):
        return self.receipts.get((task_id, input_hash))

    async def save_receipt(self, task, result):
        self.receipts[(task.task_id, task.input_hash)] = result


async def dispatched(store):
    state = make_state()
    request = ResearchRequest(task_id="market-task", company_id="acme", perspective="market",
                              as_of_date=NOW.date(), attempt=1, corpus_version="1.0")
    payload = request.model_dump(mode="json")
    ref = await store.save(payload, key="requests/market.json")
    state["active_task"] = TaskEnvelope(
        task_id="market-task", node="market", perspective="market", logical_attempt=1,
        evidence_epoch=0, payload_ref=ref, input_hash=canonical_hash(payload),
        dispatched_at=NOW,
    )
    state["research_attempts"]["market"] = 1
    return state


def empty_result(request):
    return PerspectiveResult(
        task_id=request.task_id, perspective=request.perspective, attempt=request.attempt,
        claims=[], evidence_ids=[], answered_questions={}, gaps=[],
        limitations=["No verified documents"], retrieval_receipts=[],
    )


def test_worker_receives_only_research_request_and_preserves_slots():
    async def scenario():
        store = Store()
        state = await dispatched(store)
        previous = deepcopy(state)
        async def worker(request):
            assert isinstance(request, ResearchRequest)
            assert not hasattr(request, "perspectives")
            assert not hasattr(request, "next_action")
            return empty_result(request)
        result = await make_worker_node("market", worker, store=store, now=lambda: NOW)(state)
        assert state == previous
        assert result["pending_result"].task_id == "market-task"
        assert result["step_count"] == 1
        assert result["perspectives"] == previous["perspectives"]
    asyncio.run(scenario())


def test_receipt_replay_skips_the_worker_and_counts_one_logical_step():
    async def scenario():
        store = Store()
        state = await dispatched(store)
        async def first(request):
            return empty_result(request)
        first_node = make_worker_node("market", first, store=store, now=lambda: NOW)
        completed = await first_node(state)
        async def forbidden(request):
            raise AssertionError("receipt replay must not invoke external worker")
        second_node = make_worker_node("market", forbidden, store=store, now=lambda: NOW)
        replay = await second_node(state)
        assert replay["pending_result"] == completed["pending_result"]
        assert replay["step_count"] == 1
        again = await second_node(completed)
        assert again["step_count"] == 1
    asyncio.run(scenario())


def test_task_node_or_output_task_mismatch_becomes_integrity_error():
    async def scenario():
        store = Store()
        state = await dispatched(store)
        async def wrong(request):
            return empty_result(request).model_copy(update={"task_id": "another-task"})
        result = await make_worker_node("market", wrong, store=store, now=lambda: NOW)(state)
        assert result["pending_result"].outcome == "permanent_error"
        assert result["pending_result"].error.code == "ARTIFACT_INTEGRITY_ERROR"
        assert result["perspectives"]["market"].latest_result_ref is None
        result = await make_worker_node("technology_maturity", wrong, store=store,
                                        now=lambda: NOW)(state)
        assert result["pending_result"].error.code == "ARTIFACT_INTEGRITY_ERROR"
    asyncio.run(scenario())


def test_deadline_prevents_new_worker_call_and_returns_bounded_error():
    async def scenario():
        store = Store()
        state = await dispatched(store)
        async def forbidden(request):
            raise AssertionError("deadline prevents dispatch")
        result = await make_worker_node("market", forbidden, store=store,
                                       now=lambda: NOW + timedelta(seconds=901))(state)
        assert result["pending_result"].error.code == "BUDGET_EXHAUSTED"
    asyncio.run(scenario())


def test_payload_tampering_is_not_repaired_or_used():
    async def scenario():
        store = Store()
        state = await dispatched(store)
        store.items["requests/market.json"]["company_id"] = "another-company"
        async def forbidden(request):
            raise AssertionError("tampered payload cannot invoke worker")
        result = await make_worker_node("market", forbidden, store=store, now=lambda: NOW)(state)
        assert result["pending_result"].error.code == "ARTIFACT_INTEGRITY_ERROR"
    asyncio.run(scenario())


def test_stale_reserved_epoch_never_calls_worker():
    async def scenario():
        store = Store()
        state = await dispatched(store)
        state["evidence_epoch"] = 1
        async def forbidden(request):
            raise AssertionError("A task reserved at an old epoch cannot perform work")
        result = await make_worker_node("market", forbidden, store=store, now=lambda: NOW)(state)
        assert result["pending_result"].error.code == "ARTIFACT_INTEGRITY_ERROR"
    asyncio.run(scenario())
