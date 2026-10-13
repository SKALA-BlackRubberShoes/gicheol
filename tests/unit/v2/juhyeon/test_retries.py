"""Technical retry counts, source/provider separation, and absolute deadlines."""
import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from main.runtime.neutral.errors import RuntimeFailure, classify_error
from main.runtime.neutral.retries import RetryBudget, retry_call


NOW = datetime(2026, 10, 13, tzinfo=timezone.utc)


class HTTPFailure(Exception):
    def __init__(self, status, headers=None):
        super().__init__("secret provider payload api_key=do-not-log-me")
        self.status_code, self.headers = status, headers or {}


class Clock:
    def __init__(self):
        self.value, self.sleeps = NOW, []

    def now(self):
        return self.value

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += timedelta(seconds=seconds)


@pytest.mark.parametrize("error", [HTTPFailure(429), HTTPFailure(500), HTTPFailure(503), ConnectionError("private")])
def test_transient_failure_retries_twice_without_business_state(error):
    async def scenario():
        clock, calls, budget = Clock(), [], RetryBudget()
        async def operation():
            calls.append("call")
            if len(calls) <= 2:
                raise error
            return "ok"
        result = await retry_call(operation, node="market", deadline_at=NOW + timedelta(seconds=20),
                                  now=clock.now, sleep=clock.sleep, budget=budget)
        assert result == "ok" and len(calls) == 3
        assert clock.sleeps == [1, 2]
        assert (budget.used, budget.corrections) == (2, 0)
    asyncio.run(scenario())


def test_exhaustion_is_final_to_prevent_supervisor_multiplying_retries():
    async def scenario():
        clock, calls = Clock(), []
        async def operation():
            calls.append(1)
            raise HTTPFailure(429)
        with pytest.raises(RuntimeFailure) as raised:
            await retry_call(operation, node="market", deadline_at=NOW + timedelta(seconds=20),
                             now=clock.now, sleep=clock.sleep)
        assert len(calls) == 3 and clock.sleeps == [1, 2]
        assert raised.value.error.retriable is False
        assert "do-not-log" not in str(raised.value)
    asyncio.run(scenario())


@pytest.mark.parametrize("status", [401, 403])
def test_provider_auth_is_permanent_and_never_retried(status):
    async def scenario():
        calls, clock = [], Clock()
        async def operation():
            calls.append(1)
            raise HTTPFailure(status)
        with pytest.raises(RuntimeFailure) as raised:
            await retry_call(operation, node="market", deadline_at=NOW + timedelta(seconds=20),
                             now=clock.now, sleep=clock.sleep)
        assert raised.value.error.code == "CONFIG_OR_AUTH_ERROR"
        assert not raised.value.error.retriable
        assert len(calls) == 1 and clock.sleeps == []
        assert classify_error(HTTPFailure(status), node="fetch", source=True).code == "SOURCE_INACCESSIBLE"
    asyncio.run(scenario())


@pytest.mark.parametrize("header", ["4", format_datetime(NOW + timedelta(seconds=4), usegmt=True)])
def test_retry_after_numeric_and_http_date_are_respected(header):
    async def scenario():
        clock, calls = Clock(), []
        async def operation():
            calls.append(1)
            if len(calls) == 1:
                raise HTTPFailure(429, {"Retry-After": header})
            return "ok"
        assert await retry_call(operation, node="research", deadline_at=NOW + timedelta(seconds=10),
                                now=clock.now, sleep=clock.sleep) == "ok"
        assert clock.sleeps == [4]
    asyncio.run(scenario())


def test_deadline_prevents_initial_call_or_retry_after_sleep():
    async def scenario():
        calls, clock = [], Clock()
        async def operation():
            calls.append(1)
            raise HTTPFailure(429, {"retry-after": "100"})
        for deadline, expected_calls in [(NOW, 0), (NOW + timedelta(seconds=3), 1)]:
            with pytest.raises(RuntimeFailure) as raised:
                await retry_call(operation, node="research", deadline_at=deadline,
                                 now=clock.now, sleep=clock.sleep)
            assert raised.value.error.code == "BUDGET_EXHAUSTED"
            assert len(calls) == expected_calls
        assert clock.sleeps == []
    asyncio.run(scenario())


def test_hanging_call_is_cancelled_at_absolute_deadline():
    async def scenario():
        cancelled = []
        async def operation():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)
        with pytest.raises(RuntimeFailure) as raised:
            await retry_call(operation, node="research", timeout_seconds=20,
                             deadline_at=datetime.now(timezone.utc) + timedelta(seconds=.02))
        assert raised.value.error.code == "BUDGET_EXHAUSTED" and cancelled == [True]
    asyncio.run(scenario())


def test_cancellation_is_not_retried_or_reclassified():
    async def scenario():
        async def operation():
            raise asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await retry_call(operation, node="research", deadline_at=NOW + timedelta(seconds=20), now=lambda: NOW)
    asyncio.run(scenario())
