"""Trace payloads correlate decisions without uploading prompts or source bodies."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from main.agents.supervisor.contracts import Decision
from main.runtime.neutral.errors import RuntimeFailure
from main.runtime.neutral.tracing import TraceRecorder


class Client:
    def __init__(self):
        self.calls = []

    async def create_run(self, **kwargs):
        self.calls.append(("create_run", kwargs))

    async def update_run(self, run_id, **kwargs):
        self.calls.append(("update_run", {"id": run_id, **kwargs}))


def decision(recorder, **changes):
    return Decision(**{
        "decision_id": "decision-1", "run_id": recorder.run_id, "trace_id": recorder.trace_id,
        "step": 1, "next_action": "market", "reason": "Independent market evidence is missing",
        "gap_ids": ["gap-1"], "input_state_hash": "a" * 64, "policy_version": "1.0",
        "ts": datetime(2026, 10, 13, tzinfo=timezone.utc), **changes,
    })


def test_local_decisions_persist_idempotently_without_opt_in_network(tmp_path):
    async def scenario():
        client = Client()
        recorder = TraceRecorder(tmp_path, run_id="run-1", trace_id="trace-1", client=client)
        record = decision(recorder)
        await recorder(record)
        await recorder(record)
        replay = TraceRecorder(tmp_path, run_id="run-1", trace_id="trace-1", client=client)
        await replay(record)
        lines = (tmp_path / "decisions.jsonl").read_text().splitlines()
        assert len(lines) == 1 and json.loads(lines[0])["gap_ids"] == ["gap-1"]
        assert client.calls == []
        with pytest.raises(RuntimeFailure):
            await recorder(decision(recorder, reason="Different content under the same identity"))
    asyncio.run(scenario())


def test_supervisor_decision_replay_after_ledger_write_before_checkpoint(tmp_path):
    from main.agents.supervisor.agent import Supervisor
    from policy_fixtures import new_run, Resolver, no_verdicts, NOW

    async def scenario():
        store, checkpoint_state = await new_run()
        first_recorder = TraceRecorder(tmp_path, run_id=checkpoint_state["run_id"],
                                       trace_id=checkpoint_state["trace_id"])
        first = await Supervisor(store, Resolver(), no_verdicts, now=lambda: NOW,
                                 decision_sink=first_recorder).step(checkpoint_state)
        assert first["status"] == "running"
        original_entry = (tmp_path / "decisions.jsonl").read_text()

        # Simulate process restart: the ledger is durable, but the checkpoint
        # still contains the pre-decision State, and wall-clock time advanced.
        second_recorder = TraceRecorder(tmp_path, run_id=checkpoint_state["run_id"],
                                        trace_id=checkpoint_state["trace_id"])
        replay = await Supervisor(store, Resolver(), no_verdicts,
                                  now=lambda: NOW + timedelta(seconds=1),
                                  decision_sink=second_recorder).step(checkpoint_state)
        assert replay["status"] == "running" and replay["last_error"] is None
        assert replay["last_decision_id"] == first["last_decision_id"]
        assert replay["active_task"].task_id == first["active_task"].task_id
        assert replay["eligibility_attempts"] == first["eligibility_attempts"] == 1
        assert replay["step_count"] == first["step_count"] == 1
        assert (tmp_path / "decisions.jsonl").read_text() == original_entry
        stored = json.loads(original_entry)
        with pytest.raises(RuntimeFailure, match="different contents"):
            await second_recorder(Decision.model_validate({**stored, "reason": "Changed semantic decision",
                                                          "ts": NOW + timedelta(seconds=2)}))
    asyncio.run(scenario())


def test_opt_in_trace_includes_root_node_decision_correlation_and_no_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "unusual-test-secret-123")
    async def scenario():
        client = Client()
        recorder = TraceRecorder(tmp_path, run_id=str(uuid4()), trace_id=str(uuid4()), enabled=True,
            client=client, mock=True, project_name="explicit-project", metadata={"spec_version": "1.0", "model": "explicit-model",
                "corpus_hash": "a" * 64, "api_key": "secret", "source_text": "FULL-SOURCE-TEXT",
                "payload": {"private": "FULL-PAYLOAD"}})
        await recorder.start()
        await recorder(decision(recorder, reason="Provider token=secret-marker unusual-test-secret-123"))
        async with recorder.node_span("market", task_id="task-1", perspective="market", attempt=2, gap_ids=["gap-1"]):
            pass
        await recorder.event("evidence_gate", gate_verdict="fail", evidence_ids=["ev-1"], source_text="FULL-SOURCE-TEXT")
        await recorder.finish(outcome="incomplete")
        all_text = json.dumps(client.calls, default=str) + (tmp_path / "decisions.jsonl").read_text() + (tmp_path / "trace.jsonl").read_text()
        for forbidden in ("FULL-SOURCE-TEXT", "FULL-PAYLOAD", "unusual-test-secret-123", "secret-marker"):
            assert forbidden not in all_text
        creates = [data for kind, data in client.calls if kind == "create_run"]
        assert creates[0]["extra"]["metadata"]["mock"] is True
        assert creates[0]["tags"] == ["mock"]
        assert all(data["inputs"] == {} for data in creates)
        assert all(data["project_name"] == "explicit-project" for data in creates)
        node = next(data for data in creates if data["name"] == "market")
        assert node["parent_run_id"] == recorder.trace_id
        assert node["extra"]["metadata"]["attempt"] == 2
        assert '"duration"' in all_text and '"evidence_ids"' in all_text
        status = json.loads((tmp_path / "trace_status.json").read_text())
        assert status["trace_delivery"] == "delivered" and status["mock"] is True
        assert status["delivery_error_count"] == 0
    asyncio.run(scenario())


def test_node_error_logs_generic_outcome_without_exception_payload(tmp_path):
    async def scenario():
        recorder = TraceRecorder(tmp_path, run_id="run", trace_id="trace")
        with pytest.raises(ValueError):
            async with recorder.node_span("market", task_id="task"):
                raise ValueError("exception carries FULL-SOURCE-CONTENT")
        data = (tmp_path / "trace.jsonl").read_text()
        assert '"outcome":"error"' in data
        assert "FULL-SOURCE-CONTENT" not in data
    asyncio.run(scenario())


def test_trace_delivery_failure_retains_local_decision(tmp_path):
    async def scenario():
        class Unavailable(Client):
            async def create_run(self, **kwargs):
                raise RuntimeError("API_KEY=private-remote-error")
        recorder = TraceRecorder(tmp_path, run_id="run", trace_id="trace", enabled=True, client=Unavailable())
        await recorder(decision(recorder))
        assert len((tmp_path / "decisions.jsonl").read_text().splitlines()) == 1
        assert recorder.trace_errors == ["LangSmith metadata delivery failed"]
        status = json.loads((tmp_path / "trace_status.json").read_text())
        assert status["trace_delivery"] == "failed" and status["root_created"] is False
    asyncio.run(scenario())


def test_cross_run_decision_and_symlinked_ledger_are_rejected(tmp_path):
    async def scenario():
        recorder = TraceRecorder(tmp_path / "run", run_id="run", trace_id="trace")
        with pytest.raises(RuntimeFailure):
            await recorder(decision(recorder, run_id="another-run"))
        recorder.run_dir.mkdir()
        outside = tmp_path / "outside.jsonl"
        outside.write_text("preserve")
        recorder.path.symlink_to(outside)
        with pytest.raises(RuntimeFailure):
            await recorder.start()
        assert outside.read_text() == "preserve"
    asyncio.run(scenario())
