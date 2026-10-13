"""Minimal, bounded checkpoint State for the neutral Supervisor workflow."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal
from uuid import UUID, uuid4, uuid5

from pydantic import ConfigDict, Field, TypeAdapter, ValidationError
from typing_extensions import TypedDict

from main.agents.supervisor.contracts import (
    ArtifactRef, ErrorSummary, NextAction, PERSPECTIVES, Perspective,
    PerspectiveSlot, ResultEnvelope, RunConfig, RunInput, TaskEnvelope, TerminalReason,
    canonical_hash, canonical_json,
)

Counter = Annotated[int, Field(strict=True, ge=0)]
MAX_STATE_BYTES = 32 * 1024


class SupervisorState(TypedDict):
    __pydantic_config__ = ConfigDict(extra="forbid")
    input_ref: ArtifactRef
    config_ref: ArtifactRef
    company_ref: ArtifactRef
    eligibility_ref: ArtifactRef | None
    perspectives: dict[Perspective, PerspectiveSlot]
    draft_report_ref: ArtifactRef | None
    eval_ref: ArtifactRef | None
    final_manifest_ref: ArtifactRef | None
    gate_ref: ArtifactRef | None
    repair_plan_ref: ArtifactRef | None
    run_id: str
    trace_id: str
    thread_id: str
    status: Literal["running", "completed", "incomplete", "failed"]
    terminal_reason: TerminalReason | None
    eligibility_status: Literal["pending", "eligible", "ineligible", "unknown"]
    evidence_gate_pass: bool
    active_task: TaskEnvelope | None
    pending_result: ResultEnvelope | None
    accepted_task_ids: list[str]
    next_action: NextAction
    last_decision_id: str | None
    step_count: Counter
    research_attempts: dict[Perspective, Counter]
    eligibility_attempts: Counter
    repair_cycles: Counter
    report_revision: Counter
    evidence_epoch: Counter
    active_repair_cycle_id: str | None
    resolved_gap_ids: list[str]
    node_status: dict[str, str]
    transient_retries: Counter
    last_error: ErrorSummary | None
    started_at: datetime
    deadline_at: datetime
    versions: dict[str, str]


class StateIntegrityError(ValueError):
    def __init__(self, message: str, code: str = "ARTIFACT_INTEGRITY_ERROR"):
        super().__init__(message)
        self.code = code


_adapter = TypeAdapter(SupervisorState)


def copy_state(state: SupervisorState) -> SupervisorState:
    """Copy nested dictionaries as well as models; never return a partial slot map."""
    return deepcopy(state)


def validate_state(
    state: SupervisorState, *, max_graph_steps: int | None = None,
) -> SupervisorState:
    try:
        checked = _adapter.validate_python(state)
        for identifier in ("run_id", "trace_id", "thread_id"):
            UUID(checked[identifier])
        if set(checked["perspectives"]) != set(PERSPECTIVES):
            raise ValueError("State must include all four perspective slots")
        if set(checked["research_attempts"]) != set(PERSPECTIVES):
            raise ValueError("State must include all four research counters")
        accepted = checked["accepted_task_ids"]
        if len(accepted) != len(set(accepted)):
            raise ValueError("accepted_task_ids must be unique")
        if max_graph_steps is not None and len(accepted) > max_graph_steps:
            raise ValueError("accepted_task_ids exceeds max_graph_steps")
        if len(checked["resolved_gap_ids"]) != len(set(checked["resolved_gap_ids"])):
            raise ValueError("resolved_gap_ids must be unique")
        for field in ("started_at", "deadline_at"):
            if checked[field].utcoffset() != timedelta(0):
                raise ValueError(f"{field} must be a timezone-aware UTC timestamp")
        if checked["deadline_at"] <= checked["started_at"]:
            raise ValueError("deadline must follow started_at")
        pending, active = checked["pending_result"], checked["active_task"]
        if pending is not None and active is None and pending.task_id not in accepted:
            raise ValueError("pending result has no active task")
        snapshot = _adapter.dump_python(checked, mode="json")
        if len(canonical_json(snapshot).encode("utf-8")) > MAX_STATE_BYTES:
            raise StateIntegrityError("serialized State exceeds 32 KiB", "STATE_TOO_LARGE")
        return checked
    except StateIntegrityError:
        raise
    except (ValidationError, ValueError, TypeError) as exc:
        raise StateIntegrityError(str(exc)) from exc


def state_digest(state: SupervisorState) -> str:
    return canonical_hash(_adapter.dump_python(validate_state(state), mode="json"))


def stable_task_id(
    run_id: str, node: str, perspective: str | None, logical_attempt: int,
) -> str:
    if logical_attempt < 1:
        raise ValueError("logical_attempt must be positive")
    return str(uuid5(UUID(run_id), f"{node}:{perspective or '-'}:{logical_attempt}"))


def validate_run_settings(run_input: RunInput, run_config: RunConfig, now: datetime) -> dict[str, str]:
    """Validate run prerequisites without producing side effects."""
    if now.utcoffset() != timedelta(0):
        raise ValueError("now must be timezone-aware UTC")
    if run_input.as_of_date > now.date():
        raise ValueError("as_of_date cannot be later than the execution date")
    # Routing uses policy and the current evidence verdict, not a Supervisor LLM.
    # Keep the optional field in the fingerprint so existing runs still resume.
    for field in ("research_model", "report_model", "judge_model"):
        value = getattr(run_config, field)
        if not value or not value.strip():
            raise ValueError(f"{field} model must be configured before run")
    model_ids = {key: getattr(run_config, key) for key in
                 ("supervisor_model", "research_model", "report_model", "judge_model")}
    if run_config.prompt_hash is None:
        raise ValueError("prompt_hash must bind the configured prompts before run")
    return {
        "schema": str(run_config.schema_version),
        "corpus": canonical_hash(run_input.model_dump(mode="json")["corpus_manifest"]),
        "index": run_config.index_version,
        "config": canonical_hash(run_config.model_dump(mode="json")),
        "prompt": run_config.prompt_hash,
        "policy": run_config.policy_version,
        "model": canonical_hash(model_ids),
    }


def new_state(
    run_input: RunInput, run_config: RunConfig, *,
    input_ref: ArtifactRef, config_ref: ArtifactRef, company_ref: ArtifactRef,
    now: datetime, run_id: str | None = None, trace_id: str | None = None,
    thread_id: str | None = None,
) -> SupervisorState:
    """Validate before invoking the graph, retaining only immutable artifact refs."""
    versions = validate_run_settings(run_input, run_config, now)
    state: SupervisorState = {
        "input_ref": input_ref, "config_ref": config_ref, "company_ref": company_ref,
        "eligibility_ref": None,
        "perspectives": {p: PerspectiveSlot() for p in PERSPECTIVES},
        "draft_report_ref": None, "eval_ref": None, "final_manifest_ref": None,
        "gate_ref": None, "repair_plan_ref": None,
        "run_id": run_id or str(uuid4()), "trace_id": trace_id or str(uuid4()),
        "thread_id": thread_id or str(uuid4()),
        "status": "running", "terminal_reason": None,
        "eligibility_status": "pending", "evidence_gate_pass": False,
        "active_task": None, "pending_result": None, "accepted_task_ids": [],
        "next_action": NextAction.ELIGIBILITY, "last_decision_id": None,
        "step_count": 0, "research_attempts": {p: 0 for p in PERSPECTIVES},
        "eligibility_attempts": 0, "repair_cycles": 0, "report_revision": 0,
        "evidence_epoch": 0, "active_repair_cycle_id": None, "resolved_gap_ids": [],
        "node_status": {}, "transient_retries": 0, "last_error": None,
        "started_at": now, "deadline_at": now + timedelta(seconds=run_config.run_deadline_seconds),
        "versions": versions,
    }
    return validate_state(state, max_graph_steps=run_config.max_graph_steps)
