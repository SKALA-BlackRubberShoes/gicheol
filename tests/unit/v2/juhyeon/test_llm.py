"""Structured correction and provider retries share one active-task budget."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, StrictInt

from main.runtime.neutral.errors import RuntimeFailure
from main.runtime.neutral.llm import StructuredLLMClient
from main.runtime.neutral.retries import RetryBudget, call_scope


NOW = datetime(2026, 10, 13, tzinfo=timezone.utc)


class Answer(BaseModel):
    count: StrictInt


class RateLimit(Exception):
    status_code = 429


def client_for(responses, calls, sleeps):
    async def transport(**kwargs):
        calls.append(kwargs)
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response
    async def sleep(seconds):
        sleeps.append(seconds)
    return StructuredLLMClient(transport, deadline_at=NOW + timedelta(seconds=100), now=lambda: NOW, sleep=sleep)


def test_invalid_json_receives_one_correction_with_same_explicit_model():
    async def scenario():
        calls, sleeps = [], []
        client = client_for(["bad-json", '{"count": 3}'], calls, sleeps)
        result = await client.generate(model="configured-research", system_prompt="Facts only",
                                       payload={"request": "sensitive"}, response_model=Answer)
        assert result.count == 3 and len(calls) == 2 and sleeps == []
        assert all(call["model"] == "configured-research" for call in calls)
        assert "failed schema validation" in calls[1]["system_prompt"]
        assert "bad-json" not in calls[1]["system_prompt"]
    asyncio.run(scenario())


@pytest.mark.parametrize("responses", [
    [RateLimit(), RateLimit(), "invalid"],
    ["invalid", RateLimit(), "invalid"],
])
def test_correction_and_transient_retries_cannot_exceed_three_calls(responses):
    async def scenario():
        calls, sleeps, budget = [], [], RetryBudget()
        client = client_for(list(responses), calls, sleeps)
        with call_scope(deadline_at=NOW + timedelta(seconds=100), node="market", budget=budget):
            with pytest.raises(RuntimeFailure) as raised:
                await client.generate(model="explicit", system_prompt="Prompt", payload={}, response_model=Answer)
        assert raised.value.error.code == "MODEL_OUTPUT_INVALID"
        assert not raised.value.error.retriable
        assert len(calls) == 3 and budget.used == 2 and budget.corrections <= 1
    asyncio.run(scenario())


def test_second_invalid_response_stops_even_with_spare_retry_budget():
    async def scenario():
        calls, sleeps = [], []
        client = client_for([{}, {"count": "untrusted"}], calls, sleeps)
        with pytest.raises(RuntimeFailure) as raised:
            await client.generate(model="explicit", system_prompt="Prompt", payload={}, response_model=Answer)
        assert raised.value.error.code == "MODEL_OUTPUT_INVALID" and len(calls) == 2
    asyncio.run(scenario())


def test_multiple_generations_share_active_task_budget():
    async def scenario():
        calls, sleeps, budget = [], [], RetryBudget()
        client = client_for([RateLimit(), {"count": 1}, RateLimit(), {"count": 2}, RateLimit()], calls, sleeps)
        with client.call_scope(deadline_at=NOW + timedelta(seconds=100), node="market", budget=budget):
            for expected in (1, 2):
                value = await client.generate(model="explicit", system_prompt="Prompt", payload={}, response_model=Answer)
                assert value.count == expected
            with pytest.raises(RuntimeFailure):
                await client.generate(model="explicit", system_prompt="Prompt", payload={}, response_model=Answer)
        assert len(calls) == 5 and budget.used == 2
    asyncio.run(scenario())


@pytest.mark.parametrize("missing", ["model", "deadline"])
def test_missing_explicit_model_or_original_deadline_fails_before_transport(missing):
    async def scenario():
        calls = []
        async def transport(**kwargs):
            calls.append(kwargs)
        client = StructuredLLMClient(transport, deadline_at=None if missing == "deadline" else NOW, now=lambda: NOW)
        with pytest.raises(RuntimeFailure) as raised:
            await client.generate(model="" if missing == "model" else "explicit", system_prompt="Prompt",
                                  payload={}, response_model=Answer)
        assert raised.value.error.code == "CONFIG_OR_AUTH_ERROR" and calls == []
    asyncio.run(scenario())


def test_ambient_langsmith_prompt_tracing_is_disabled_inside_transport():
    async def scenario():
        from langsmith import tracing_context, get_tracing_context
        async def transport(**kwargs):
            assert get_tracing_context()["enabled"] is False
            return {"count": 2}
        client = StructuredLLMClient(transport, deadline_at=NOW + timedelta(seconds=20), now=lambda: NOW)
        with tracing_context(enabled=True):
            result = await client.generate(model="explicit", system_prompt="private", payload={}, response_model=Answer)
        assert result.count == 2
    asyncio.run(scenario())


def test_openai_sdk_uses_explicit_model_and_disables_hidden_retries():
    async def scenario():
        calls = []
        class SDK:
            def __init__(self):
                self.chat = SimpleNamespace(completions=self)
            def with_options(self, **kwargs):
                calls.append(("options", kwargs))
                return self
            async def create(self, **kwargs):
                calls.append(("create", kwargs))
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"count": 4}'))])
        client = StructuredLLMClient(client=SDK(), deadline_at=NOW + timedelta(seconds=20), now=lambda: NOW)
        result = await client.generate(model="requested-model", system_prompt="Prompt", payload={}, response_model=Answer)
        assert result.count == 4
        assert calls[0][1]["max_retries"] == 0
        assert calls[1][1]["model"] == "requested-model"
        assert calls[1][1]["response_format"]["json_schema"]["name"] == "Answer"
    asyncio.run(scenario())


def test_langchain_injected_client_cannot_hide_retries_or_change_model():
    async def scenario():
        for sdk in [SimpleNamespace(model_name="different-model", max_retries=0),
                    SimpleNamespace(model_name="requested-model", max_retries=2)]:
            client = StructuredLLMClient(client=sdk, backend="langchain",
                deadline_at=NOW + timedelta(seconds=20), now=lambda: NOW)
            with pytest.raises(RuntimeFailure) as raised:
                await client.generate(model="requested-model", system_prompt="Prompt", payload={}, response_model=Answer)
            assert raised.value.error.code == "CONFIG_OR_AUTH_ERROR"
    asyncio.run(scenario())
