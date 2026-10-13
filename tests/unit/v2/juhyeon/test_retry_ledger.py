"""Issue #25: provider reservations survive cancellation and task replay."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import BaseModel

from main.runtime.neutral.errors import RuntimeFailure
from main.runtime.neutral.llm import StructuredLLMClient
from main.runtime.neutral.retries import call_scope
from main.runtime.neutral.retry_ledger import RetryLedger
from main.storage.artifacts import ArtifactIntegrityError, FileArtifactStore


class Answer(BaseModel):
    value: int


class RateLimit(Exception):
    status_code = 429


async def no_sleep(seconds):
    pass


def budget_for(path, *, input_hash="a" * 64, limit=2):
    return RetryLedger(FileArtifactStore(path, run_id="run")).task_budget(
        task_id="task", input_hash=input_hash, max_retries=limit)


async def generate(client, budget, *, payload=None):
    with call_scope(deadline_at=datetime.now(timezone.utc) + timedelta(seconds=30),
                    node="market", budget=budget):
        return await client.generate(model="fixture", system_prompt="fixture",
                                     payload=payload or {}, response_model=Answer)


def test_exhausted_calls_cannot_restart_after_external_cancel_and_store_reopen(tmp_path):
    async def scenario():
        calls = []
        stalled = asyncio.Event()
        async def transport(**kwargs):
            calls.append(1)
            if len(calls) < 3:
                raise RateLimit()
            stalled.set()
            await asyncio.Event().wait()
        client = StructuredLLMClient(transport, sleep=no_sleep)
        task = asyncio.create_task(generate(client, budget_for(tmp_path)))
        await asyncio.wait_for(stalled.wait(), timeout=10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(calls) == 3
        for _ in range(2):
            with pytest.raises(RuntimeFailure) as raised:
                await generate(client, budget_for(tmp_path))
            assert not raised.value.error.retriable
        assert len(calls) == 3
    asyncio.run(scenario())


def test_successful_logical_generations_replay_without_duplicate_provider_calls(tmp_path):
    async def scenario():
        calls = []
        async def transport(**kwargs):
            calls.append(kwargs["payload"])
            return {"value": len(calls)}
        client = StructuredLLMClient(transport)
        for _ in range(2):
            budget = budget_for(tmp_path)
            with call_scope(deadline_at=datetime.now(timezone.utc) + timedelta(seconds=30),
                            node="writer", budget=budget):
                # More than three legitimate initial generations are allowed.
                for index in range(4):
                    result = await client.generate(model="fixture", system_prompt="fixture",
                        payload={"index": index}, response_model=Answer)
                    assert result.value == index + 1
        assert len(calls) == 4
    asyncio.run(scenario())


def test_correction_remains_shared_across_cancel_and_replay(tmp_path):
    async def scenario():
        prompts = []
        stalled = asyncio.Event()
        async def transport(**kwargs):
            prompts.append(kwargs["system_prompt"])
            if len(prompts) == 1:
                return "invalid"
            if len(prompts) == 2:
                stalled.set()
                await asyncio.Event().wait()
            return "still invalid"
        client = StructuredLLMClient(transport, sleep=no_sleep)
        task = asyncio.create_task(generate(client, budget_for(tmp_path)))
        await asyncio.wait_for(stalled.wait(), timeout=10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(RuntimeFailure):
            await generate(client, budget_for(tmp_path))
        assert len(prompts) == 3
        assert "previous response failed" in prompts[1] and prompts[2] == prompts[1]
        with pytest.raises(RuntimeFailure):
            await generate(client, budget_for(tmp_path))
        assert len(prompts) == 3
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["input", "payload", "limit"])
def test_task_or_logical_request_cannot_change_during_replay(tmp_path, change):
    async def scenario():
        calls = []
        async def transport(**kwargs):
            calls.append(1)
            return {"value": 1}
        client = StructuredLLMClient(transport)
        await generate(client, budget_for(tmp_path))
        budget = budget_for(tmp_path, input_hash="b" * 64 if change == "input" else "a" * 64,
                            limit=1 if change == "limit" else 2)
        with pytest.raises(ArtifactIntegrityError):
            await generate(client, budget, payload={"changed": True} if change == "payload" else {})
        assert len(calls) == 1
    asyncio.run(scenario())


def test_reservations_are_atomic_across_concurrent_store_instances(tmp_path):
    async def scenario():
        calls = []
        stalled = asyncio.Event()
        async def transport(**kwargs):
            calls.append(1)
            if len(calls) == 3:
                stalled.set()
            await asyncio.Event().wait()
        client = StructuredLLMClient(transport)
        async def execute():
            try:
                await generate(client, budget_for(tmp_path))
            except (asyncio.CancelledError, RuntimeFailure):
                pass
        tasks = [asyncio.create_task(execute()) for _ in range(8)]
        await asyncio.wait_for(stalled.wait(), timeout=10)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks)
        assert len(calls) == 3
    asyncio.run(scenario())


def test_multiple_logical_calls_share_retry_budget_after_completed_call_replay(tmp_path):
    async def scenario():
        calls = []
        async def transport(**kwargs):
            calls.append(kwargs["payload"]["index"])
            if len(calls) in {1, 3, 5}:
                raise RateLimit()
            return {"value": kwargs["payload"]["index"]}
        client = StructuredLLMClient(transport, sleep=no_sleep)
        for _ in range(2):
            with call_scope(deadline_at=datetime.now(timezone.utc) + timedelta(seconds=30),
                            node="market", budget=budget_for(tmp_path)):
                for index in range(2):
                    result = await client.generate(model="fixture", system_prompt="fixture",
                        payload={"index": index}, response_model=Answer)
                    assert result.value == index
                with pytest.raises(RuntimeFailure):
                    await client.generate(model="fixture", system_prompt="fixture",
                        payload={"index": 2}, response_model=Answer)
        assert calls == [0, 0, 1, 1, 2]
    asyncio.run(scenario())


@pytest.mark.parametrize("terminal", ["auth", "schema"])
def test_terminal_error_is_not_retried_if_callback_receipt_was_not_saved(tmp_path, terminal):
    async def scenario():
        calls = []
        async def transport(**kwargs):
            calls.append(1)
            if terminal == "auth":
                exc = Exception("secret")
                exc.status_code = 401
                raise exc
            return "invalid"
        client = StructuredLLMClient(transport, sleep=no_sleep)
        for _ in range(3):
            with pytest.raises(RuntimeFailure) as raised:
                await generate(client, budget_for(tmp_path))
            assert not raised.value.error.retriable
        assert len(calls) == (1 if terminal == "auth" else 2)
    asyncio.run(scenario())


def test_provider_does_not_run_if_durable_reservation_cannot_be_saved(tmp_path, monkeypatch):
    async def scenario():
        calls = []
        async def transport(**kwargs):
            calls.append(1)
            return {"value": 1}
        def unavailable(*args, **kwargs):
            raise OSError("write unavailable")
        monkeypatch.setattr(FileArtifactStore, "_write", unavailable)
        with pytest.raises(ArtifactIntegrityError):
            await generate(StructuredLLMClient(transport), budget_for(tmp_path))
        assert not calls
    asyncio.run(scenario())


@pytest.mark.parametrize("corrupt", [b"{broken", b"null", b"[]", b"1", b'"text"'])
def test_corrupt_retry_ledger_is_an_integrity_error_before_provider_call(tmp_path, corrupt):
    async def scenario():
        calls = []
        async def transport(**kwargs):
            calls.append(1)
            return {"value": 1}
        client = StructuredLLMClient(transport)
        await generate(client, budget_for(tmp_path))
        ledger_path = next((tmp_path / ".runtime/retries").glob("*.json"))
        ledger_path.write_bytes(corrupt)
        with pytest.raises(ArtifactIntegrityError):
            await generate(client, budget_for(tmp_path))
        assert len(calls) == 1
        assert ledger_path.read_bytes() == corrupt
    asyncio.run(scenario())


def test_pending_schema_correction_survives_cancel_before_dispatch(tmp_path, monkeypatch):
    async def scenario():
        prompts = []
        async def transport(**kwargs):
            prompts.append(kwargs["system_prompt"])
            return "invalid" if len(prompts) == 1 else {"value": 1}
        client = StructuredLLMClient(transport)
        original = RetryLedger.update
        cancelled = False
        async def interrupt(self, task, change):
            nonlocal cancelled
            result = await original(self, task, change)
            # Stop after the correction intent was durably written, before the
            # next call is reserved. No remote correction has started yet.
            if change.__name__ == "available" and not cancelled:
                cancelled = True
                raise asyncio.CancelledError()
            return result
        monkeypatch.setattr(RetryLedger, "update", interrupt)
        with pytest.raises(asyncio.CancelledError):
            await generate(client, budget_for(tmp_path))
        result = await generate(client, budget_for(tmp_path))
        assert result.value == 1 and len(prompts) == 2
        assert "previous response failed" in prompts[1]
    asyncio.run(scenario())


def test_success_cannot_overwrite_a_persisted_terminal_error(tmp_path):
    async def scenario():
        operation = await budget_for(tmp_path).operation("signature")
        await operation.reserve_call(node="market")
        await operation.fail({"code": "CONFIG_OR_AUTH_ERROR", "node": "market",
                              "message": "Authentication failed", "retriable": False})
        with pytest.raises(ArtifactIntegrityError):
            await operation.complete({"value": 1})
    asyncio.run(scenario())
