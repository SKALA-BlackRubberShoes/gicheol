"""AT11 injects real runtime retries through the existing Supervisor boundary."""
import asyncio
from datetime import timedelta

import pytest

from main.agents.supervisor.agent import Supervisor
from main.agents.supervisor.contracts import RunConfig
from main.graph.supervisor_nodes import make_worker_node
from main.runtime.neutral.retries import retry_call
from test_nodes import Store, dispatched, empty_result
from test_state import NOW


class ProviderHTTPError(Exception):
    def __init__(self, status):
        super().__init__("sensitive provider response must never reach the receipt")
        self.status_code = status
        self.headers = {"Retry-After": "1"}


@pytest.mark.parametrize("injected,expected_status,expected_reason,expected_calls", [
    ("429_then_success", "running", None, 2),
    ("401", "failed", "CONFIG_OR_AUTH_ERROR", 1),
    ("429_exhausted", "failed", "MODEL_OUTPUT_INVALID", 3),
    ("deadline", "incomplete", "BUDGET_EXHAUSTED", 0),
])
def test_at11_runtime_retry_receipts_produce_expected_terminal_state(injected, expected_status, expected_reason, expected_calls):
    async def scenario():
        store = Store()
        original = await dispatched(store)
        calls, waits = [], []
        async def sleep(seconds):
            waits.append(seconds)
        async def worker(request):
            async def provider():
                calls.append(1)
                if injected == "401":
                    raise ProviderHTTPError(401)
                if injected.startswith("429") and (len(calls) == 1 or injected == "429_exhausted"):
                    raise ProviderHTTPError(429)
                return empty_result(request)
            return await retry_call(provider, node="market", deadline_at=original["deadline_at"],
                                    now=lambda: NOW, sleep=sleep)
        now = (lambda: original["deadline_at"] + timedelta(seconds=1)) if injected == "deadline" else (lambda: NOW)
        result = await make_worker_node("market", worker, store=store, now=now)(original)
        assert len(calls) == expected_calls
        assert result["research_attempts"]["market"] == original["research_attempts"]["market"] == 1
        assert result["step_count"] == 1
        supervisor = Supervisor(store, None, None, now=now)
        await supervisor.accept(result, RunConfig())
        assert result["status"] == expected_status
        assert result["terminal_reason"] == expected_reason
        assert result["active_task"] is None
        assert result["transient_retries"] == 0
        if result["last_error"]:
            assert "sensitive provider" not in result["last_error"].message
    asyncio.run(scenario())


def test_at11_sufficiency_retry_after_exceeding_deadline_is_incomplete(monkeypatch):
    from pydantic import BaseModel
    from main.runtime.neutral import services
    from main.runtime.neutral.llm import StructuredLLMClient
    from policy_fixtures import new_run, full_fixture, Resolver, contracts

    class Response(BaseModel):
        checked: bool

    async def scenario():
        c = contracts()
        store, state = await new_run()
        _, _, evidence_bundle, _, _ = await full_fixture(store, state)
        calls = []
        async def transport(**kwargs):
            calls.append(kwargs["model"])
            exc = ProviderHTTPError(429)
            exc.headers = {"Retry-After": "1000"}  # Original run has 900 seconds.
            raise exc
        llm = StructuredLLMClient(transport, now=lambda: NOW)
        async def assess(request):
            return await llm.generate(model="fixture-judge", system_prompt="Check support",
                                      payload={}, response_model=Response)
        async def unused(request):
            pytest.fail("Budget exhaustion cannot dispatch another business worker")
        bundle = services.ServiceBundle(workers={name: unused for name in
            ["eligibility", "writer", "evaluator", *(p.value for p in c.PERSPECTIVES)]},
            evidence_resolver=Resolver(evidence_bundle), assess_evidence=assess,
            publish_worker=unused, mock=True)
        monkeypatch.setattr(services, "build_supervisor_graph", lambda **kwargs: kwargs["supervisor"])
        config = c.RunConfig.model_validate(await store.load(state["config_ref"]))
        supervisor = services.assemble_graph(bundle=bundle, store=store, run_config=config,
            checkpointer=object(), deadline_at=state["deadline_at"], now=lambda: NOW)
        result = await supervisor.step(state)
        assert (result["status"], result["terminal_reason"]) == ("incomplete", "BUDGET_EXHAUSTED")
        assert result["next_action"] == "abort" and result["final_manifest_ref"] is None
        assert result["research_attempts"] == state["research_attempts"]
        assert calls == ["fixture-judge"]
    asyncio.run(scenario())
