"""Durable local decisions and opt-in, metadata-only LangSmith traces."""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import tempfile
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from uuid import uuid4

from main.agents.supervisor.contracts import Decision, canonical_json
from .errors import failure


_FIELDS = frozenset({
    "run_id", "trace_id", "spec_version", "schema_version", "corpus_hash", "index_hash",
    "index_revision", "config_hash", "prompt_hash", "policy_version", "model", "models",
    "supervisor_model", "research_model", "report_model", "judge_model", "mock",
    "task_id", "perspective", "attempt", "duration", "outcome", "gap_ids", "node",
    "decision_id", "step", "next_action", "reason", "input_state_hash", "ts",
    "gate_verdict", "evidence_ids", "event", "span_id",
    "trace_delivery", "supervisor", "research", "report", "judge",
})
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_ -]?key|authorization|password|secret|access[_ -]?token|refresh[_ -]?token|token)"
    r"\s*[:=]\s*(?:bearer\s+)?[^\s,;]+"
)
_SECRET_TOKEN = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{6,}|lsv2_[A-Za-z0-9_-]{6,}|Bearer\s+[^\s,;]+)", re.I)


def redact_text(value: str) -> str:
    for name, secret in os.environ.items():
        if len(secret) >= 6 and any(part in name.upper() for part in ("API_KEY", "TOKEN", "PASSWORD", "SECRET")):
            value = value.replace(secret, "[REDACTED]")
    value = _SECRET_ASSIGNMENT.sub(lambda match: match.group(1) + "=[REDACTED]", value)
    value = _SECRET_TOKEN.sub("[REDACTED]", value)
    return value


def sanitize_metadata(metadata: dict | None) -> dict:
    """Allow known operational fields only; prompts, payloads and source text drop."""
    def clean(value):
        if isinstance(value, Enum):
            value = value.value
        if isinstance(value, datetime):
            value = value.isoformat()
        if isinstance(value, str):
            return redact_text(value)[:2000]
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            return value if value == value and abs(value) != float("inf") else None
        if isinstance(value, (list, tuple)):
            return [clean(item) for item in value[:100] if not isinstance(item, (dict, list, tuple))]
        if isinstance(value, dict):
            return {str(k): clean(v) for k, v in value.items() if k in _FIELDS}
        return None
    return {key: clean(value) for key, value in (metadata or {}).items() if key in _FIELDS}


class TraceRecorder:
    """Async decision sink with explicit, optional external trace emission.

    External tracing failures are reported in ``trace_errors`` without leaking
    provider text or turning an otherwise valid evaluation into a success/fail.
    Local decision persistence failures remain fatal and are never swallowed.
    """
    def __init__(self, run_dir, *, run_id: str, trace_id: str, metadata=None,
                 enabled: bool = False, client=None, mock: bool = False,
                 project_name: str | None = None):
        self.run_dir = Path(run_dir)
        self.run_id, self.trace_id = run_id, trace_id
        self.enabled, self.client = bool(enabled), client
        self.project_name = project_name
        self.metadata = sanitize_metadata({**(metadata or {}), "run_id": run_id,
                                           "trace_id": trace_id, "mock": bool(mock)})
        self.path = self.run_dir / "decisions.jsonl"
        self.events_path = self.run_dir / "trace.jsonl"
        self.trace_errors: list[str] = []
        self._lock = asyncio.Lock()
        self._started = False
        self._remote_started = False

    def _write_status(self):
        """Persist delivery evidence separately from a successful workflow outcome."""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        destination = self.run_dir / "trace_status.json"
        if destination.is_symlink():
            raise failure("ARTIFACT_INTEGRITY_ERROR", "tracing", "Trace status must not be a symbolic link")
        status = "disabled" if not self.enabled else "failed" if self.trace_errors else "delivered" if self._remote_started else "pending"
        payload = {"run_id": redact_text(self.run_id), "trace_id": redact_text(self.trace_id),
                   "enabled": self.enabled, "mock": self.metadata["mock"], "trace_delivery": status,
                   "root_created": self._remote_started, "delivery_error_count": len(self.trace_errors)}
        path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.run_dir,
                                             prefix=".trace-status-", delete=False) as stream:
                path = Path(stream.name)
                stream.write(canonical_json(payload) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(path, destination)
        finally:
            if path is not None:
                path.unlink(missing_ok=True)

    async def _remote(self, method: str, *args, **kwargs):
        if not self.enabled:
            return False
        try:
            if self.client is None:
                from langsmith import Client
                self.client = Client(auto_batch_tracing=False, timeout_ms=4000,
                                     hide_inputs=True, hide_outputs=True,
                                     omit_traced_runtime_info=True, tracing_sampling_rate=1.0)
            call = getattr(self.client, method)
            if method == "create_run" and self.project_name is not None:
                kwargs.setdefault("project_name", self.project_name)
            async with asyncio.timeout(5):
                if inspect.iscoroutinefunction(call):
                    await call(*args, **kwargs)
                else:
                    result = await asyncio.to_thread(call, *args, **kwargs)
                    if inspect.isawaitable(result):
                        await result
            return True
        except Exception:
            self.trace_errors.append("LangSmith metadata delivery failed")
            self._write_status()
            return False

    def _append(self, path: Path, payload: dict):
        self.run_dir.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            raise failure("ARTIFACT_INTEGRITY_ERROR", "tracing", "Trace paths must not be symbolic links")
        with path.open("a", encoding="utf-8") as stream:
            stream.write(canonical_json(payload) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    async def start(self):
        async with self._lock:
            if self._started:
                return self.trace_id
            self.run_dir.mkdir(parents=True, exist_ok=True)
            if self.path.is_symlink():
                raise failure("ARTIFACT_INTEGRITY_ERROR", "tracing", "Trace paths must not be symbolic links")
            self.path.touch(exist_ok=True)
            self._append(self.events_path, {"event": "root_start", **self.metadata})
            self._started = True
        self._remote_started = await self._remote(
            "create_run", name="neutral-evaluation", run_type="chain", id=self.trace_id,
            trace_id=self.trace_id, inputs={}, extra={"metadata": self.metadata},
            tags=["mock"] if self.metadata["mock"] else ["live"],
        )
        self._write_status()
        return self.trace_id

    async def __call__(self, decision: Decision):
        decision = Decision.model_validate(decision.model_dump(mode="json"))
        if decision.run_id != self.run_id or decision.trace_id != self.trace_id:
            raise failure("ARTIFACT_INTEGRITY_ERROR", "tracing", "Decision does not belong to this run and trace")
        if not self._started:
            await self.start()
        payload = sanitize_metadata(decision.model_dump(mode="json"))
        async with self._lock:
            try:
                lines = self.path.read_text(encoding="utf-8").splitlines()
                previous = [json.loads(line) for line in lines if line.strip()]
            except (OSError, ValueError):
                raise failure("ARTIFACT_INTEGRITY_ERROR", "tracing", "The decision ledger could not be verified") from None
            for item in previous:
                if item.get("decision_id") == decision.decision_id:
                    # A decision may be durable before its checkpoint commits.
                    # Replaying the same incoming State recomputes only the
                    # operational timestamp; retain the original ledger entry.
                    if ({key: value for key, value in item.items() if key != "ts"}
                            != {key: value for key, value in payload.items() if key != "ts"}):
                        raise failure("ARTIFACT_INTEGRITY_ERROR", "tracing", "Decision identity was reused with different contents")
                    return
            self._append(self.path, payload)
        if self._remote_started:
            span_id = str(uuid4())
            created = await self._remote("create_run", name="supervisor-decision", run_type="chain",
                id=span_id, parent_run_id=self.trace_id, trace_id=self.trace_id,
                inputs={}, extra={"metadata": payload})
            if created:
                await self._remote("update_run", span_id, outputs={}, end_time=datetime.now(timezone.utc))

    async def event(self, event: str, **metadata):
        """Record gate verdicts/evidence IDs and other approved operational fields."""
        payload = sanitize_metadata({**metadata, "event": event, "run_id": self.run_id, "trace_id": self.trace_id})
        async with self._lock:
            self._append(self.events_path, payload)
            if "gate_verdict" in payload or "evidence_ids" in payload:
                self._append(self.path, payload)
        if self._remote_started:
            span_id = str(uuid4())
            created = await self._remote("create_run", name="runtime-event", run_type="chain",
                id=span_id, parent_run_id=self.trace_id, trace_id=self.trace_id,
                inputs={}, extra={"metadata": payload})
            if created:
                await self._remote("update_run", span_id, outputs={}, end_time=datetime.now(timezone.utc))

    @asynccontextmanager
    async def node_span(self, node: str, *, task_id: str, perspective=None, attempt: int = 1, gap_ids=()):
        if not self._started:
            await self.start()
        started, span_id, outcome = time.monotonic(), str(uuid4()), "success"
        metadata = sanitize_metadata({"node": node, "task_id": task_id, "perspective": perspective,
                                      "attempt": attempt, "gap_ids": list(gap_ids), "span_id": span_id,
                                      "run_id": self.run_id, "trace_id": self.trace_id})
        created = self._remote_started and await self._remote(
            "create_run", name=redact_text(node)[:256], run_type="chain", id=span_id,
            parent_run_id=self.trace_id, trace_id=self.trace_id, inputs={}, extra={"metadata": metadata})
        try:
            yield metadata
        except BaseException:
            outcome = "error"
            raise
        finally:
            result = sanitize_metadata({**metadata, "duration": time.monotonic() - started, "outcome": outcome})
            async with self._lock:
                self._append(self.events_path, {"event": "node_end", **result})
            if created:
                await self._remote("update_run", span_id, outputs={}, extra={"metadata": result},
                                   end_time=datetime.now(timezone.utc), error="Node failed" if outcome == "error" else None)

    async def finish(self, *, outcome: str):
        if not self._started:
            await self.start()
        await self.event("root_end", outcome=outcome)
        if self._remote_started:
            await self._remote("update_run", self.trace_id, outputs={},
                               extra={"metadata": sanitize_metadata({**self.metadata, "outcome": outcome})},
                               end_time=datetime.now(timezone.utc))
        self._write_status()
