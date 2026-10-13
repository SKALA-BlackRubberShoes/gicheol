"""Issue #25 through the real wrapper, receipt storage, and SQLite resume."""
import asyncio
from datetime import datetime, timedelta, timezone

from langgraph.errors import NodeCancelledError
from pydantic import BaseModel
from policy_fixtures import new_run, Resolver

from main.agents.supervisor import contracts as c
from main.runtime.neutral.llm import StructuredLLMClient
from main.runtime.neutral.services import ServiceBundle, assemble_graph
from main.storage.artifacts import FileArtifactStore
from main.storage.checkpoints import open_checkpointer


class Answer(BaseModel):
    value: bool


class RateLimit(Exception):
    status_code = 429


def test_outer_timeout_receipt_replay_after_sqlite_reopen_keeps_original_budget(tmp_path):
    async def scenario():
        memory, state = await new_run({"llm_timeout_seconds": 1, "max_transient_retries": 2})
        state["started_at"] = datetime.now(timezone.utc)
        state["deadline_at"] = state["started_at"] + timedelta(seconds=900)
        store = FileArtifactStore(tmp_path / state["run_id"], run_id=state["run_id"])
        for key, payload in memory.payloads.items():
            await store.save(payload, key=key)
        config = c.RunConfig.model_validate(await store.load(state["config_ref"]))
        calls, tasks = [], []
        async def no_sleep(seconds):
            pass
        async def transport(**kwargs):
            calls.append(1)
            if len(calls) < 3:
                raise RateLimit()
            await asyncio.Event().wait()
        llm = StructuredLLMClient(transport, sleep=no_sleep)
        async def eligibility(request):
            tasks.append(request.task_id)
            await llm.generate(model="fixture", system_prompt="fixture",
                               payload={}, response_model=Answer)
            raise AssertionError("Stalled operation cannot produce a result")
        async def unused(request):
            raise AssertionError("Failed eligibility cannot dispatch downstream services")
        bundle = ServiceBundle(workers={"eligibility": eligibility, "writer": unused,
            "evaluator": unused, **{p.value: unused for p in c.PERSPECTIVES}},
            evidence_resolver=Resolver(), assess_evidence=unused, publish_worker=unused, mock=True)
        save_receipt = store.save_receipt
        async def crash_after_transient_receipt(task, result):
            await save_receipt(task, result)
            assert result.outcome == "transient_error" and result.error.retriable
            assert task.logical_attempt == 1
            raise asyncio.CancelledError()
        store.save_receipt = crash_after_transient_receipt
        invocation = {"configurable": {"thread_id": state["thread_id"]}}
        async with open_checkpointer(tmp_path / "checkpoints.sqlite") as saver:
            graph = assemble_graph(bundle=bundle, store=store, run_config=config,
                checkpointer=saver, deadline_at=state["deadline_at"])
            try:
                await graph.ainvoke(state, invocation)
            except (asyncio.CancelledError, NodeCancelledError):
                pass
            else:
                raise AssertionError("Crash injection must interrupt the graph")
        assert len(calls) == 3 and len(tasks) == 1
        # Recreate both storage and graph: no in-memory budget survives here.
        reopened = FileArtifactStore(store.root, run_id=state["run_id"])
        async with open_checkpointer(tmp_path / "checkpoints.sqlite") as saver:
            graph = assemble_graph(bundle=bundle, store=reopened, run_config=config,
                checkpointer=saver, deadline_at=state["deadline_at"])
            result = await graph.ainvoke(None, invocation)
        assert (result["status"], result["terminal_reason"]) == ("failed", "MODEL_OUTPUT_INVALID")
        assert len(calls) == 3 and tasks == [tasks[0], tasks[0]]
        assert result["research_attempts"] == state["research_attempts"]
        assert result["transient_retries"] == 0
        assert len(result["accepted_task_ids"]) == 1
    asyncio.run(scenario())
