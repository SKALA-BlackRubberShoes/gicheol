"""Sanitized runtime failures using the Supervisor's existing error contract."""
from __future__ import annotations

from typing import Any

from main.agents.supervisor.contracts import ErrorSummary, TerminalReason
from main.graph.supervisor_nodes import WorkerFailure


class RuntimeFailure(WorkerFailure):
    """A classified failure understood by the existing worker boundary."""


class ModelOutputFailure(RuntimeFailure):
    """Structured output is invalid and may receive one format correction."""


def failure(code: TerminalReason | str, node: str, message: str, *, retriable: bool = False) -> RuntimeFailure:
    return RuntimeFailure(ErrorSummary(code=code, node=node, retriable=retriable, message=message))


def deadline_failure(node: str) -> RuntimeFailure:
    return failure("BUDGET_EXHAUSTED", node, "The original run deadline has no remaining budget")


def status_code(exc: BaseException) -> int | None:
    value = getattr(exc, "status_code", None)
    if value is None:
        value = getattr(getattr(exc, "response", None), "status_code", None)
    if value is None:
        value = getattr(exc, "status", None)  # aiohttp
    if value is None:
        value = getattr(exc, "code", None)  # urllib HTTPError
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def response_headers(exc: BaseException) -> Any:
    return getattr(exc, "headers", None) or getattr(getattr(exc, "response", None), "headers", {}) or {}


def classify_error(exc: BaseException, *, node: str, source: bool = False) -> ErrorSummary:
    """Never include provider exception text, request bodies, or credentials.

    ``source=True`` applies only to fetching an individual source; authentication
    failures from a search/model provider must retain the default provider scope.
    """
    if isinstance(exc, WorkerFailure):
        return exc.error
    code = status_code(exc)
    if source and code in {401, 402, 403, 451}:
        return ErrorSummary(code="SOURCE_INACCESSIBLE", node=node, retriable=False,
                            message="The individual source cannot be accessed; use an alternative source")
    if code in {401, 403}:
        return ErrorSummary(code="CONFIG_OR_AUTH_ERROR", node=node, retriable=False,
                            message="Provider authentication or authorization failed")
    # Provider SDKs expose network errors under different names. Inspect class
    # ancestry, not raw exception strings, which often contain URLs and secrets.
    names = {kind.__name__ for kind in type(exc).__mro__}
    network = isinstance(exc, (ConnectionError, TimeoutError)) or bool(names & {
        "APIConnectionError", "APITimeoutError", "ConnectError", "ConnectTimeout",
        "ReadError", "ReadTimeout", "WriteError", "WriteTimeout", "PoolTimeout",
        "NetworkError", "RemoteProtocolError", "ClientConnectionError",
        "ConnectionError", "Timeout", "ChunkedEncodingError", "ProtocolError", "gaierror",
    })
    if code == 429 or code is not None and 500 <= code <= 599 or network:
        return ErrorSummary(code="MODEL_OUTPUT_INVALID", node=node, retriable=True,
                            message="A transient provider or network call failed")
    if isinstance(exc, (ImportError, PermissionError)) or code is not None and 400 <= code <= 499:
        return ErrorSummary(code="CONFIG_OR_AUTH_ERROR", node=node, retriable=False,
                            message="The configured provider or request could not be used")
    return ErrorSummary(code="MODEL_OUTPUT_INVALID", node=node, retriable=False,
                        message="The service did not return a usable result")
