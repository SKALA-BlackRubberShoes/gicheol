"""Task-scoped retry reservations survive callback replay and checkpoint loss.

A reservation is fsynced before a provider can run. Interrupted reservations are
never refunded: after a crash we cannot know whether the remote call completed.
Successful logical generations are cached so replay does not repeat earlier
calls in a worker that legitimately uses the LLM more than once.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
import threading

from main.agents.supervisor.contracts import ErrorSummary, canonical_hash, canonical_json
from main.storage.artifacts import ArtifactIntegrityError
from .errors import RuntimeFailure, failure


class RetryLedger:
    def __init__(self, store, *, mock: bool = False):
        self.store = store
        self.durable = all(callable(getattr(store, name, None)) for name in
                           ("_transaction", "_exists", "_read", "_write"))
        if not self.durable and not mock:
            raise ValueError("Production services require durable retry reservation storage")
        if not self.durable:
            # Explicit mock bundles may use their existing in-memory test store.
            # Keep the ledger on that store across graph reconstruction.
            if not hasattr(store, "_neutral_retry_records"):
                store._neutral_retry_records = ({}, threading.Lock())

    def task_budget(self, *, task_id: str, input_hash: str, max_retries: int):
        return TaskRetryBudget(self, task_id, input_hash, max_retries)

    async def update(self, task, change):
        if self.durable:
            return await asyncio.to_thread(self._update, task, change)
        return self._update(task, change)

    def _update(self, task, change):
        key = f".runtime/retries/{canonical_hash({'task_id': task.task_id})}.json"
        identity = {"run_id": getattr(self.store, "run_id", None),
                    "task_id": task.task_id, "input_hash": task.input_hash,
                    "max_retries": task.max_retries}
        if self.durable:
            lock = self.store._transaction()
        else:
            records, lock = self.store._neutral_retry_records
        with lock:
            if self.durable:
                try:
                    present = self.store._exists(key)
                    raw = json.loads(self.store._read(key)) if present else None
                except (ValueError, UnicodeError) as exc:
                    raise ArtifactIntegrityError("Task retry ledger is not valid JSON") from exc
            else:
                present = key in records
                raw = deepcopy(records.get(key))
            if not present:
                record = {**identity, "used": 0, "corrections": 0, "operations": []}
            else:
                try:
                    record = raw["record"]
                    if set(raw) != {"record", "sha256"} or raw["sha256"] != canonical_hash(record):
                        raise ValueError("retry record hash differs")
                    if set(record) != {*identity, "used", "corrections", "operations"}:
                        raise ValueError("retry record fields differ")
                    if any(record.get(k) != v for k, v in identity.items()):
                        raise ValueError("retry task identity differs")
                    if type(record["used"]) is not int or not 0 <= record["used"] <= task.max_retries:
                        raise ValueError("invalid retry count")
                    if type(record["corrections"]) is not int or not 0 <= record["corrections"] <= min(1, record["used"]):
                        raise ValueError("invalid correction count")
                    if not isinstance(record["operations"], list):
                        raise ValueError("invalid operations")
                    for op in record["operations"]:
                        if (set(op) != {"signature", "calls", "corrected", "pending_correction", "completed", "result", "error"}
                            or type(op["calls"]) is not int or not 0 <= op["calls"] <= 1 + task.max_retries
                            or type(op["corrected"]) is not bool or type(op["completed"]) is not bool
                            or type(op["pending_correction"]) is not bool
                            or not isinstance(op["signature"], str)):
                            raise ValueError("invalid operation")
                        if op["error"] is not None:
                            ErrorSummary.model_validate(op["error"])
                            if op["completed"]:
                                raise ValueError("operation cannot succeed and fail")
                    if record["used"] != sum(max(0, op["calls"] - 1) for op in record["operations"]):
                        raise ValueError("retry count differs from reservations")
                    if record["corrections"] != sum(op["corrected"] for op in record["operations"]):
                        raise ValueError("correction count differs from reservations")
                except (KeyError, TypeError, ValueError) as exc:
                    raise ArtifactIntegrityError("Task retry ledger integrity failed") from exc
            result = change(record)
            saved = {"record": record, "sha256": canonical_hash(record)}
            if self.durable:
                self.store._write(key, canonical_json(saved).encode("utf-8"))
            else:
                records[key] = deepcopy(saved)
            return result


class TaskRetryBudget:
    def __init__(self, ledger, task_id, input_hash, max_retries):
        self.ledger, self.task_id, self.input_hash = ledger, task_id, input_hash
        self.max_retries, self.ordinal = max_retries, 0

    async def operation(self, signature: str):
        ordinal = self.ordinal
        self.ordinal += 1
        def begin(record):
            operations = record["operations"]
            if ordinal > len(operations):
                raise ArtifactIntegrityError("Logical LLM operation order differs on replay")
            if ordinal == len(operations):
                operations.append({"signature": signature, "calls": 0, "corrected": False, "pending_correction": False,
                                   "completed": False, "result": None, "error": None})
            op = operations[ordinal]
            if op["signature"] != signature:
                raise ArtifactIntegrityError("Logical LLM request differs on task replay")
            return deepcopy(op), record["used"]
        op, used = await self.ledger.update(self, begin)
        return OperationRetryBudget(self, ordinal, op, used)


class OperationRetryBudget:
    def __init__(self, task, ordinal, state, used):
        self.task, self.ordinal, self.state, self.used = task, ordinal, state, used

    @property
    def retry_number(self):
        # Unlike ephemeral budgets, a retry is charged at dispatch, not while
        # deciding to sleep. Cancellation during backoff must not refund calls.
        return self.used + 1

    async def prepare_retry(self, *, correction: bool = False) -> bool:
        def available(record):
            self.used = record["used"]
            allowed = (record["used"] < self.task.max_retries
                       and (not correction or record["corrections"] < 1))
            if correction and allowed:
                record["operations"][self.ordinal]["pending_correction"] = True
            return allowed
        return await self.task.ledger.update(self.task, available)

    async def reserve_call(self, *, node: str) -> None:
        def reserve(record):
            op = record["operations"][self.ordinal]
            if op["error"] is not None:
                raise RuntimeFailure(ErrorSummary.model_validate(op["error"]))
            if op["completed"]:
                raise ArtifactIntegrityError("Concurrent replay of a completed LLM operation")
            if op["calls"]:
                if record["used"] >= self.task.max_retries:
                    raise failure("MODEL_OUTPUT_INVALID", node, "The shared technical retry budget is exhausted")
                if op["pending_correction"] and record["corrections"] >= 1:
                    raise failure("MODEL_OUTPUT_INVALID", node, "The structured correction budget is exhausted")
                record["used"] += 1
                if op["pending_correction"]:
                    record["corrections"] += 1
                    op["corrected"] = True
                    op["pending_correction"] = False
            op["calls"] += 1
            return record["used"]
        self.used = await self.task.ledger.update(self.task, reserve)

    async def complete(self, result):
        def finish(record):
            op = record["operations"][self.ordinal]
            if op["error"] is not None:
                raise ArtifactIntegrityError("Concurrent LLM failure and success differ")
            if op["completed"] and op["result"] != result:
                raise ArtifactIntegrityError("Concurrent LLM results differ")
            op["completed"], op["result"] = True, result
        await self.task.ledger.update(self.task, finish)

    async def fail(self, error):
        """Keep terminal provider/schema errors final even before a receipt."""
        def finish(record):
            op = record["operations"][self.ordinal]
            if op["completed"]:
                raise ArtifactIntegrityError("Concurrent LLM success and failure differ")
            if op["error"] is None:
                op["error"] = error
        await self.task.ledger.update(self.task, finish)
