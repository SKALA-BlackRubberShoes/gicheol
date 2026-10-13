"""Finite async technical retries, independent of business attempt counters."""
from __future__ import annotations

import asyncio
import math
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Awaitable, Callable, TypeVar

from .errors import RuntimeFailure, classify_error, deadline_failure, response_headers

T = TypeVar("T")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class RetryBudget:
    """One shared budget for transient retries and structured-output correction."""
    max_retries: int = 2
    used: int = 0
    corrections: int = 0

    def __post_init__(self):
        if type(self.max_retries) is not int or self.max_retries < 0:
            raise ValueError("max_retries must be a nonnegative integer")
        if self.used != 0 or self.corrections != 0:
            raise ValueError("a new retry budget must start unused")

    def consume(self, *, correction: bool = False) -> bool:
        if self.used >= self.max_retries or correction and self.corrections >= 1:
            return False
        self.used += 1
        self.corrections += int(correction)
        return True

    async def reserve_call(self, *, node: str) -> None:
        """Durable budgets override this to reserve before provider dispatch."""

    async def prepare_retry(self, *, correction: bool = False) -> bool:
        return self.consume(correction=correction)

    @property
    def retry_number(self) -> int:
        return self.used


@dataclass(frozen=True)
class CallScope:
    deadline_at: datetime
    node: str
    budget: RetryBudget


_scope: ContextVar[CallScope | None] = ContextVar("neutral_call_scope", default=None)


def current_scope() -> CallScope | None:
    return _scope.get()


@contextmanager
def call_scope(*, deadline_at: datetime, node: str = "llm", budget: RetryBudget | None = None):
    """Bind the original deadline and shared budget to one active task."""
    if deadline_at.tzinfo is None or deadline_at.utcoffset() is None:
        raise ValueError("deadline_at must include a timezone")
    token = _scope.set(CallScope(deadline_at, node, budget if budget is not None else RetryBudget()))
    try:
        yield _scope.get()
    finally:
        _scope.reset(token)


def remaining_seconds(deadline_at: datetime, now: Callable[[], datetime], node: str) -> float:
    if deadline_at.tzinfo is None or deadline_at.utcoffset() is None:
        raise ValueError("deadline_at must include a timezone")
    remaining = (deadline_at - now()).total_seconds()
    if remaining <= 0:
        raise deadline_failure(node)
    return remaining


def retry_after_seconds(exc: BaseException, *, now: datetime) -> float:
    headers = response_headers(exc)
    value = next((v for k, v in headers.items() if str(k).lower() == "retry-after"), None)
    if value is None:
        return 0.0
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        try:
            parsed = parsedate_to_datetime(str(value))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            seconds = (parsed - now).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return 0.0
    return max(0.0, seconds) if math.isfinite(seconds) else 0.0


async def retry_call(
    factory: Callable[[], Awaitable[T]], *, node: str, deadline_at: datetime,
    timeout_seconds: float = 30, budget: RetryBudget | None = None,
    source: bool = False, now: Callable[[], datetime] = utc_now,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive and finite")
    budget = budget if budget is not None else RetryBudget()
    while True:
        remaining = remaining_seconds(deadline_at, now, node)
        await budget.reserve_call(node=node)
        remaining = remaining_seconds(deadline_at, now, node)
        timer = asyncio.timeout(min(timeout_seconds, remaining))
        try:
            async with timer:
                result = await factory()
            remaining_seconds(deadline_at, now, node)
            return result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if now() >= deadline_at or timer.expired() and remaining <= timeout_seconds:
                raise deadline_failure(node) from None
            error = classify_error(exc, node=node, source=source)
            if not error.retriable:
                if isinstance(exc, RuntimeFailure):
                    raise
                raise RuntimeFailure(error) from None
            if not await budget.prepare_retry():
                # The Supervisor also understands transient receipts. Mark an
                # exhausted runtime budget final so it cannot multiply retries
                # by replaying this same task with a fresh adapter budget.
                raise RuntimeFailure(error.model_copy(update={
                    "retriable": False, "message": "The shared technical retry budget is exhausted",
                })) from None
            delay = max(float(2 ** (budget.retry_number - 1)), retry_after_seconds(exc, now=now()))
            remaining = remaining_seconds(deadline_at, now, node)
            if delay >= remaining:
                raise deadline_failure(node) from None
            # An injected sleeper can support deterministic tests, but even a
            # broken sleeper must not outlive the real run budget.
            try:
                async with asyncio.timeout(remaining):
                    await sleep(delay)
            except TimeoutError:
                raise deadline_failure(node) from None
