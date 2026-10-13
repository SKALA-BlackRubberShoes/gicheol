"""Real disk/SQLite recovery with abrupt child-process exits (no memory saver)."""
import asyncio
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys

from langgraph.graph import END, START, StateGraph
import pytest

from main.agents.supervisor import contracts as c
from main.graph.supervisor_nodes import make_worker_node
from main.graph.supervisor_state import StateIntegrityError, SupervisorState, stable_task_id
from main.graph.supervisor_workflow import initialize_run
from main.storage.artifacts import FileArtifactStore
from main.storage.checkpoints import load_resume_state, open_checkpointer

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "unit/v2/juhyeon"))
from test_storage_artifacts import make_manifest


async def persisted_run(root):
    store = FileArtifactStore(root)
    config = c.RunConfig(supervisor_model="fixture-supervisor", research_model="fixture-research",
        report_model="fixture-writer", judge_model="fixture-judge", prompt_hash="a" * 64)
    run_input = c.RunInput(company_name="Acme", official_url="https://example.com", domain="Energy",
        as_of_date="2026-01-01", corpus_manifest=[])
    state = await initialize_run(run_input, config, store=store)
    task_id = stable_task_id(state["run_id"], "eligibility", None, 1)
    request = c.EligibilityRequest(task_id=task_id, company_id="acme", company_name="Acme",
        official_url="https://example.com", as_of_date="2026-01-01", attempt=1)
    task = c.TaskEnvelope(task_id=task_id, node="eligibility", logical_attempt=1,
        evidence_epoch=0, payload_ref=await store.save(request, key="task-input.json"),
        input_hash=c.canonical_hash(request), dispatched_at=datetime.now(timezone.utc))
    state.update(active_task=task, eligibility_attempts=1, step_count=5)
    state["research_attempts"][c.Perspective.MARKET] = 1
    state["node_status"]["market"] = "done:previous-market-task"
    await store.save(task, key="reserved-task.json")
    graph = StateGraph(SupervisorState)
    graph.add_node("reserve", lambda current: current)
    graph.add_edge(START, "reserve")
    graph.add_edge("reserve", END)
    async with open_checkpointer(root / "checkpoints.sqlite") as saver:
        await graph.compile(checkpointer=saver).ainvoke(state,
            {"configurable": {"thread_id": state["thread_id"]}})
    return store, state, config


def run_child(source, *arguments):
    result = subprocess.run([sys.executable, "-c", source, *(str(arg) for arg in arguments)],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 77, result.stderr


def test_at09_worker_receipt_survives_process_exit_before_checkpoint(tmp_path):
    async def scenario():
        store, original, config = await persisted_run(tmp_path)
        run_child('''
import asyncio, json, os, sys
from pathlib import Path
from main.storage.artifacts import FileArtifactStore
from main.agents.supervisor.contracts import TaskEnvelope, EligibilityResult, ResultEnvelope
async def main():
    store = FileArtifactStore(sys.argv[1])
    task = TaskEnvelope.model_validate_json((Path(sys.argv[1]) / "reserved-task.json").read_text())
    result = EligibilityResult(task_id=task.task_id, unlisted=True, funding_stage=True,
        exit_status=True, verdict="eligible", claims=[], gaps=[])
    ref = await store.save(result, key=f"results/{task.task_id}.json")
    await store.save_receipt(task, ResultEnvelope(task_id=task.task_id, outcome="success", result_ref=ref))
    os._exit(77)
asyncio.run(main())
''', tmp_path)
        async with open_checkpointer(tmp_path / "checkpoints.sqlite") as saver:
            recovered = await load_resume_state(saver, original["thread_id"], original["versions"])
            assert recovered["pending_result"] is None
            assert isinstance(recovered["active_task"], c.TaskEnvelope)
            calls = []
            async def forbidden_worker(request):
                calls.append(request)
                raise AssertionError("completed worker must not be called after restart")
            node = make_worker_node("eligibility", forbidden_worker, store=FileArtifactStore(tmp_path), limits=config)
            replayed = await node(recovered)
            assert calls == []
            assert replayed["pending_result"].outcome == "success"
            assert replayed["active_task"].task_id == recovered["active_task"].task_id
            assert replayed["step_count"] == 6
            assert replayed["eligibility_attempts"] == 1
            assert replayed["research_attempts"] == original["research_attempts"]
            assert replayed["node_status"]["market"] == "done:previous-market-task"
            assert len(list((tmp_path / "results").glob("*.json"))) == 1
            assert len(list((tmp_path / "receipts").glob("*.json"))) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("version", ["corpus", "prompt", "config", "model", "index", "policy", "schema"])
def test_at15_sqlite_reopen_rejects_changed_version(tmp_path, version):
    async def scenario():
        _, original, _ = await persisted_run(tmp_path)
        async with open_checkpointer(tmp_path / "checkpoints.sqlite") as saver:
            recovered = await load_resume_state(saver, original["thread_id"], original["versions"])
            assert recovered["versions"] == original["versions"]
            changed = dict(original["versions"], **{version: "different-version"})
            with pytest.raises(StateIntegrityError) as error:
                await load_resume_state(saver, original["thread_id"], changed)
            assert error.value.code == "RESUME_VERSION_MISMATCH"
            unchanged = await load_resume_state(saver, original["thread_id"], original["versions"])
            assert unchanged == recovered
    asyncio.run(scenario())


def test_missing_sqlite_thread_cannot_silently_start_new_run(tmp_path):
    async def scenario():
        async with open_checkpointer(tmp_path / "checkpoints.sqlite") as saver:
            with pytest.raises(StateIntegrityError, match="no persisted checkpoint"):
                await load_resume_state(saver, "absent-thread", {})
    asyncio.run(scenario())


@pytest.mark.parametrize("crash_point", ["before_manifest", "after_manifest"])
def test_at19_pdf_publication_across_abrupt_process_exit(tmp_path, crash_point):
    async def scenario():
        store = FileArtifactStore(tmp_path, "run-1")
        candidate = await make_manifest(store)
        await store.save(candidate, key="candidate.json")
        run_child('''
import asyncio, os, sys
from pathlib import Path
from main.storage.artifacts import FileArtifactStore
from main.agents.supervisor.contracts import Manifest
async def main():
    store = FileArtifactStore(sys.argv[1], "run-1")
    manifest = Manifest.model_validate_json((Path(sys.argv[1]) / "candidate.json").read_text())
    write = store._write
    def crash_before_manifest(key, data, media_type="application/json"):
        if key == "manifest.json":
            os._exit(77)
        return write(key, data, media_type)
    if sys.argv[2] == "before_manifest":
        store._write = crash_before_manifest
    await store.commit_manifest(manifest)
    os._exit(77)
asyncio.run(main())
''', tmp_path, crash_point)
        reopened = FileArtifactStore(tmp_path, "run-1")
        previous = await reopened.find_manifest("run-1", 1)
        if crash_point == "before_manifest":
            assert previous is None
            assert await reopened.approved_report_path("run-1", 1) is None
            assert (tmp_path / candidate.report_pdf_ref.relative_path).exists()
            assert (tmp_path / ".storage/manifest-ref.json").exists()
        else:
            assert previous is not None
        committed = await reopened.commit_manifest(candidate)
        assert committed == await reopened.find_manifest("run-1", 1)
        if previous is not None:
            assert committed == previous
        assert await reopened.approved_report_path("run-1", 1) == tmp_path / candidate.report_pdf_ref.relative_path
        assert len(list(tmp_path.rglob("*.pdf"))) == 1
        assert len(list(tmp_path.glob("manifest.json"))) == 1
    asyncio.run(scenario())
