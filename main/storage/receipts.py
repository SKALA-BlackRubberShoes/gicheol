"""Stable task receipts; reused results must still match their original inputs."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

from main.agents.supervisor.contracts import (
    ResultEnvelope, TaskEnvelope, canonical_hash, canonical_json,
)

if TYPE_CHECKING:
    from .artifacts import FileArtifactStore


class ReceiptStore:
    """Synchronous implementation used inside FileArtifactStore worker threads."""
    def __init__(self, store: FileArtifactStore):
        self.store = store

    @staticmethod
    def key(task_id: str) -> str:
        return f"receipts/{canonical_hash({'task_id': task_id})}.json"

    def _validate(self, task: TaskEnvelope, result: ResultEnvelope) -> None:
        from .artifacts import ArtifactIntegrityError
        if result.task_id != task.task_id:
            raise ArtifactIntegrityError("receipt result belongs to another task")
        payload = self.store._load(task.payload_ref)
        if canonical_hash(payload) != task.input_hash:
            raise ArtifactIntegrityError("receipt task input hash differs from saved payload")
        self.store._verify_embedded_refs(payload)
        if result.result_ref is not None:
            output = self.store._load(result.result_ref)
            self.store._verify_embedded_refs(output)
            if "task_id" in output and output["task_id"] != task.task_id:
                raise ArtifactIntegrityError("result artifact belongs to another task")

    def _read(self, key: str):
        from .artifacts import ArtifactIntegrityError
        try:
            raw = json.loads(self.store._read(key))
            if set(raw) != {"task", "result", "sha256"}:
                raise ValueError("invalid receipt fields")
            if raw["sha256"] != canonical_hash({"task": raw["task"], "result": raw["result"]}):
                raise ValueError("receipt hash mismatch")
            task = TaskEnvelope.model_validate(raw["task"])
            result = ResultEnvelope.model_validate(raw["result"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ArtifactIntegrityError("receipt integrity failed") from exc
        self._validate(task, result)
        return task, result

    def find(self, task_id: str, input_hash: str) -> ResultEnvelope | None:
        from .artifacts import ArtifactIntegrityError
        key = self.key(task_id)
        with self.store._transaction():
            if not self.store._exists(key):
                return None
            task, result = self._read(key)
            if task.task_id != task_id:
                raise ArtifactIntegrityError("receipt key differs from task identity")
            if task.input_hash != input_hash:
                raise ArtifactIntegrityError("same task_id cannot be replayed with changed inputs")
            return result

    def save(self, task: TaskEnvelope, result: ResultEnvelope) -> None:
        from .artifacts import ArtifactIntegrityError
        task = TaskEnvelope.model_validate(task.model_dump(mode="json"))
        result = ResultEnvelope.model_validate(result.model_dump(mode="json"))
        key = self.key(task.task_id)
        with self.store._transaction():
            self._validate(task, result)
            if self.store._exists(key):
                previous_task, previous_result = self._read(key)
                if previous_task != task:
                    raise ArtifactIntegrityError("task identity cannot be rebound to another input")
                if previous_result == result:
                    return
                if previous_result.outcome != "transient_error":
                    raise ArtifactIntegrityError("completed task receipt cannot be replaced")
            payload = {"task": task.model_dump(mode="json"), "result": result.model_dump(mode="json")}
            record = dict(payload, sha256=canonical_hash(payload))
            self.store._write(key, canonical_json(record).encode("utf-8"))
