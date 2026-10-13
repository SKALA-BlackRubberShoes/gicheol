"""DTO-only workers, replay receipts, and terminal boundaries.

Storage and worker implementations are injected. The wrapper never invokes
another worker or modifies perspective/control acceptance slots.
"""
from __future__ import annotations

import inspect
from datetime import datetime, timezone
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from main.agents.supervisor.contracts import (
    ArtifactStore, DraftReport, EligibilityRequest, EligibilityResult,
    ErrorSummary, EvalVerdict, EvaluatorRequest, GateCertificate, Manifest,
    PERSPECTIVES, PerspectiveResult, PublishRequest, PublishResult,
    ResearchRequest, ResultEnvelope, RunConfig, TerminalReason, WriterRequest,
    canonical_hash,
)
from main.agents.supervisor.policy import terminal_status
from main.agents.supervisor.agent import bounded_call, bounded_store, ServiceTimeout
from .supervisor_state import (
    SupervisorState, StateIntegrityError, copy_state, stable_task_id, validate_state,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class WorkerFailure(Exception):
    """A runtime adapter may return a classified error without leaking raw text."""
    def __init__(self, error: ErrorSummary):
        super().__init__(error.message)
        self.error = error


_REQUEST_TYPES = {
    "eligibility": EligibilityRequest, "writer": WriterRequest,
    "evaluator": EvaluatorRequest,
    **{p.value: ResearchRequest for p in PERSPECTIVES},
}
_RESULT_TYPES = {
    "eligibility": EligibilityResult, "writer": DraftReport,
    "evaluator": EvalVerdict,
    **{p.value: PerspectiveResult for p in PERSPECTIVES},
}


def _data(value: Any) -> dict:
    return value.model_dump(mode="json") if isinstance(value, BaseModel) else value


def _error(task_id: str, node: str, code: str, message: str,
           *, retriable: bool = False) -> ResultEnvelope:
    return ResultEnvelope(
        task_id=task_id, outcome="transient_error" if retriable else "permanent_error",
        error=ErrorSummary(code=code, node=node, retriable=retriable, message=message),
    )


def _matches_task(task, request, result) -> bool:
    if getattr(request, "task_id", None) != task.task_id:
        return False
    if isinstance(result, (EligibilityResult, PerspectiveResult)) and result.task_id != task.task_id:
        return False
    if isinstance(request, ResearchRequest):
        if request.perspective != task.perspective or request.attempt != task.logical_attempt:
            return False
        if isinstance(result, PerspectiveResult) and (
            result.perspective != task.perspective or result.attempt != task.logical_attempt
        ):
            return False
    if isinstance(result, DraftReport):
        return result.revision == task.report_revision and result.evidence_epoch == task.evidence_epoch
    if isinstance(result, EvalVerdict):
        return (result.report_revision == task.report_revision and
                result.report_hash == request.report.content_hash and
                result.evidence_epoch == task.evidence_epoch)
    return True


def make_worker_node(
    node: str, worker: Callable, *, store: ArtifactStore,
    now: Callable[[], datetime] = utc_now, limits: RunConfig | None = None,
):
    if node not in _REQUEST_TYPES:
        raise ValueError("node must be a registered business worker")
    limits = limits or RunConfig()

    async def run(state: SupervisorState) -> SupervisorState:
        state = validate_state(state, max_graph_steps=limits.max_graph_steps)
        updated = copy_state(state)
        task = state["active_task"]
        if task is None:
            raise StateIntegrityError("worker has no reserved active task")
        already_done = state["node_status"].get(node) == f"done:{task.task_id}"
        if already_done and state["pending_result"] is not None:
            return updated
        envelope = None
        io = bounded_store(store, state, limits=limits, now=now)
        try:
            if task.node.value != node:
                raise StateIntegrityError("worker node differs from reserved task")
            if task.evidence_epoch != state["evidence_epoch"]:
                raise StateIntegrityError("worker task was reserved against an obsolete evidence epoch")
            if state["step_count"] >= limits.max_graph_steps or now() >= state["deadline_at"]:
                envelope = _error(task.task_id, node, "BUDGET_EXHAUSTED",
                                  "No remaining step or deadline budget for worker")
            else:
                payload = await io.load(task.payload_ref)
                request = _REQUEST_TYPES[node].model_validate(payload)
                if canonical_hash(request.model_dump(mode="json")) != task.input_hash:
                    raise StateIntegrityError("reserved task input hash differs from payload")
                if not _matches_task(task, request, None):
                    raise StateIntegrityError("reserved task differs from worker request")
                if node in {"writer", "evaluator"}:
                    from main.agents.supervisor.agent import validate_current_gate, validate_writer_inputs, validate_evaluator_inputs
                    if not await validate_current_gate(state, io, config=limits):
                        raise StateIntegrityError("report worker requires a current evidence gate")
                    guard = validate_writer_inputs if node == "writer" else validate_evaluator_inputs
                    if not await guard(state, io, request):
                        raise StateIntegrityError("report inputs differ from gate-approved artifacts")
                if node == "writer":
                    if (request.report_revision != state["report_revision"] or
                        request.evidence_epoch != state["evidence_epoch"]):
                        raise StateIntegrityError("writer request is stale")
                receipt = await io.find_receipt(task.task_id, task.input_hash)
                if receipt is not None and receipt.outcome != "transient_error":
                    envelope = ResultEnvelope.model_validate(_data(receipt))
                    if envelope.task_id != task.task_id:
                        raise StateIntegrityError("receipt differs from active task")
                    if envelope.result_ref is not None:
                        saved = _RESULT_TYPES[node].model_validate(await io.load(envelope.result_ref))
                        if not _matches_task(task, request, saved):
                            raise StateIntegrityError("receipt result differs from active task")
                else:
                    if not (inspect.iscoroutinefunction(worker) or
                            inspect.iscoroutinefunction(getattr(worker, "__call__", None))):
                        raise WorkerFailure(ErrorSummary(code="CONFIG_OR_AUTH_ERROR", node=node,
                            retriable=False, message="Worker must implement the async DTO interface"))
                    remaining = (state["deadline_at"] - now()).total_seconds()
                    if remaining <= 0:
                        raise WorkerFailure(ErrorSummary(code="BUDGET_EXHAUSTED", node=node,
                            retriable=False, message="Deadline expired before worker invocation"))
                    try:
                        raw = await bounded_call(state, limits.llm_timeout_seconds,
                                                 lambda: worker(request), now=now)
                    except ServiceTimeout as exc:
                        raise WorkerFailure(ErrorSummary(
                            code="BUDGET_EXHAUSTED" if exc.deadline_exhausted else "MODEL_OUTPUT_INVALID",
                            node=node, retriable=not exc.deadline_exhausted,
                            message="Worker call exceeded its allowed timeout")) from exc
                    if isinstance(raw, ResultEnvelope):
                        envelope = ResultEnvelope.model_validate(_data(raw))
                        if envelope.task_id != task.task_id:
                            raise StateIntegrityError("worker error envelope differs from active task")
                        if envelope.result_ref is not None:
                            saved = _RESULT_TYPES[node].model_validate(await io.load(envelope.result_ref))
                            if not _matches_task(task, request, saved):
                                raise StateIntegrityError("worker result differs from active task")
                    else:
                        # First check identity even for model_copy() values that bypass validators.
                        if isinstance(raw, BaseModel) and not _matches_task(task, request, raw):
                            raise StateIntegrityError("worker result identity is stale or unrelated")
                        result = _RESULT_TYPES[node].model_validate(_data(raw))
                        if not _matches_task(task, request, result):
                            raise StateIntegrityError("worker result differs from reserved task")
                        ref = await io.save(result.model_dump(mode="json"),
                                               key=f"results/{task.task_id}.json")
                        outcome = "empty" if isinstance(result, PerspectiveResult) and not result.claims else "success"
                        envelope = ResultEnvelope(task_id=task.task_id, outcome=outcome, result_ref=ref)
        except StateIntegrityError:
            envelope = _error(task.task_id, node, "ARTIFACT_INTEGRITY_ERROR",
                              "Task payload, result, receipt, or gate integrity failed")
        except ValidationError:
            envelope = _error(task.task_id, node, "MODEL_OUTPUT_INVALID",
                              "Worker output does not satisfy the registered DTO")
        except WorkerFailure as exc:
            envelope = ResultEnvelope(task_id=task.task_id,
                outcome="transient_error" if exc.error.retriable else "permanent_error",
                error=exc.error)
        except ServiceTimeout as exc:
            envelope = _error(task.task_id, node,
                "BUDGET_EXHAUSTED" if exc.deadline_exhausted else "ARTIFACT_INTEGRITY_ERROR",
                "Artifact operation exceeded its allowed timeout")
        except TimeoutError:
            expired = now() >= state["deadline_at"]
            envelope = _error(task.task_id, node,
                "BUDGET_EXHAUSTED" if expired else "MODEL_OUTPUT_INVALID",
                "Worker call exceeded its allowed timeout", retriable=not expired)
        except (OSError, KeyError, ValueError):
            envelope = _error(task.task_id, node, "ARTIFACT_INTEGRITY_ERROR",
                              "Required artifact or receipt could not be verified")
        except Exception:
            envelope = _error(task.task_id, node, "MODEL_OUTPUT_INVALID",
                              "Worker service failed without a classified runtime error")
        try:
            await io.save_receipt(task, envelope)
        except ServiceTimeout as exc:
            # Return a bounded error even when the receipt store itself stalls.
            if envelope.error is None or envelope.error.code != "BUDGET_EXHAUSTED":
                envelope = _error(task.task_id, node,
                    "BUDGET_EXHAUSTED" if exc.deadline_exhausted else "ARTIFACT_INTEGRITY_ERROR",
                    "Task receipt could not be persisted within its budget")
        except Exception:
            envelope = _error(task.task_id, node, "ARTIFACT_INTEGRITY_ERROR",
                              "Task receipt could not be persisted")
        updated["pending_result"] = envelope
        updated["node_status"][node] = f"done:{task.task_id}"
        # Task replay / technical retries must not consume another logical step.
        if not already_done and state["step_count"] < limits.max_graph_steps:
            updated["step_count"] += 1
        return validate_state(updated, max_graph_steps=limits.max_graph_steps)

    return run


def make_publish_node(
    worker: Callable, *, store: ArtifactStore, now: Callable[[], datetime] = utc_now,
    limits: RunConfig | None = None,
):
    limits = limits or RunConfig()

    async def publish(state: SupervisorState) -> SupervisorState:
        from main.agents.supervisor.agent import (
            current_evaluation, current_report, validate_current_gate,
        )
        state = validate_state(state, max_graph_steps=limits.max_graph_steps)
        updated = copy_state(state)
        io = bounded_store(store, state, limits=limits, now=now)
        try:
            # A completed commit is an existing result, not a new work node.
            # After deadline, permit only finite read-only lookup/validation.
            if now() >= state["deadline_at"]:
                io = bounded_store(store, state, limits=limits, now=now, completion_recovery=True)
            ref = state["final_manifest_ref"] or await io.find_manifest(
                state["run_id"], state["report_revision"])
            if ref is not None:
                io = bounded_store(store, state, limits=limits, now=now, completion_recovery=True)
            elif now() >= state["deadline_at"]:
                raise WorkerFailure(ErrorSummary(code="BUDGET_EXHAUSTED", node="publish",
                    retriable=False, message="No committed publication exists before deadline exhaustion"))
            if not await validate_current_gate(state, io, config=limits):
                raise StateIntegrityError("publication requires a current evidence gate")
            report = await current_report(state, io)
            evaluation = await current_evaluation(state, io, report)
            if report is None or evaluation is None or not evaluation.overall_pass:
                raise StateIntegrityError("publication requires latest passing report evaluation")
            eligibility = EligibilityResult.model_validate(await io.load(state["eligibility_ref"]))
            results = {p: PerspectiveResult.model_validate(await io.load(slot.latest_result_ref))
                       for p, slot in state["perspectives"].items()}
            gate = GateCertificate.model_validate(await io.load(state["gate_ref"]))
            request = PublishRequest(
                task_id=stable_task_id(state["run_id"], "publish", None, report.revision),
                run_id=state["run_id"], report=report, evaluation=evaluation,
                gate=gate, eligibility=eligibility, perspective_results=results,
            )
            outcome = None
            if ref is None:
                if now() >= state["deadline_at"] or state["step_count"] >= limits.max_graph_steps:
                    raise WorkerFailure(ErrorSummary(code="BUDGET_EXHAUSTED", node="publish",
                        retriable=False, message="No budget remains for PDF publication"))
                if not (inspect.iscoroutinefunction(worker) or
                        inspect.iscoroutinefunction(getattr(worker, "__call__", None))):
                    raise StateIntegrityError("publish worker must implement async DTO interface")
                try:
                    raw = await bounded_call(state, limits.llm_timeout_seconds,
                                             lambda: worker(request), now=now)
                except ServiceTimeout as exc:
                    code = "BUDGET_EXHAUSTED" if exc.deadline_exhausted else "PDF_LAYOUT_FAILED"
                    raise WorkerFailure(ErrorSummary(code=code, node="publish", retriable=False,
                        message="PDF publication exceeded its allowed timeout")) from exc
                outcome = PublishResult.model_validate(_data(raw))
                if (outcome.report_revision, outcome.report_hash, outcome.evidence_epoch) != (
                    report.revision, report.content_hash, state["evidence_epoch"]
                ) or not outcome.pdf_validation_pass:
                    raise StateIntegrityError("PDF validation result does not bind the evaluated draft")
                ref = outcome.final_manifest_ref
                if ref is None:
                    manifest = Manifest(
                        run_id=state["run_id"], status="completed",
                        report_revision=report.revision, report_hash=report.content_hash,
                        evidence_epoch=state["evidence_epoch"],
                        eligibility_hash=eligibility.result_hash,
                        perspective_hashes={p: r.result_hash for p, r in results.items()},
                        gate_ref=state["gate_ref"], draft_ref=state["draft_report_ref"],
                        evaluation_ref=state["eval_ref"], report_pdf_ref=outcome.staging_pdf_ref,
                        pdf_validation_pass=True, trace_id=state["trace_id"],
                        versions=state["versions"], committed_at=now(),
                    )
                    # Storage validates files and commits the manifest LAST, atomically.
                    ref = await io.commit_manifest(manifest)
            # A returned committed ref may arrive at the deadline boundary.
            # Verify that existing result with finite reads; never start work.
            io = bounded_store(store, state, limits=limits, now=now, completion_recovery=True)
            try:
                committed = Manifest.model_validate(await io.load(ref))
            except ValidationError as exc:
                raise StateIntegrityError("Committed manifest does not satisfy its artifact contract") from exc
            await io.verify(committed.report_pdf_ref)
            if (committed.run_id, committed.report_revision, committed.report_hash,
                committed.evidence_epoch, committed.eligibility_hash) != (
                state["run_id"], report.revision, report.content_hash,
                state["evidence_epoch"], eligibility.result_hash
            ):
                raise StateIntegrityError("committed manifest does not match the current run")
            if (committed.perspective_hashes != gate.perspective_hashes or
                committed.gate_ref != state["gate_ref"] or
                committed.draft_ref != state["draft_report_ref"] or
                committed.evaluation_ref != state["eval_ref"] or
                committed.versions != state["versions"] or committed.trace_id != state["trace_id"]):
                raise StateIntegrityError("manifest references do not bind current artifacts")
            if outcome is not None and outcome.staging_pdf_ref is not None and committed.report_pdf_ref != outcome.staging_pdf_ref:
                raise StateIntegrityError("manifest PDF differs from the validated staging artifact")
            updated.update(status="completed", final_manifest_ref=ref, terminal_reason=None,
                           active_task=None, pending_result=None)
        except WorkerFailure as exc:
            updated.update(status=terminal_status(exc.error.code), terminal_reason=exc.error.code,
                           last_error=exc.error, final_manifest_ref=None)
        except ServiceTimeout as exc:
            code = "BUDGET_EXHAUSTED" if exc.deadline_exhausted else "ARTIFACT_INTEGRITY_ERROR"
            updated.update(status=terminal_status(code), terminal_reason=code, final_manifest_ref=None,
                last_error=ErrorSummary(code=code, node="publish", retriable=False,
                                       message="Publication artifact operation exceeded its allowed timeout"))
        except TimeoutError:
            code = "BUDGET_EXHAUSTED" if now() >= state["deadline_at"] else "PDF_LAYOUT_FAILED"
            updated.update(status=terminal_status(code), terminal_reason=code, final_manifest_ref=None,
                last_error=ErrorSummary(code=code, node="publish", retriable=False,
                                       message="PDF publication exceeded its allowed timeout"))
        except (StateIntegrityError, OSError, KeyError, ValueError) as exc:
            code = "PDF_LAYOUT_FAILED" if isinstance(exc, ValidationError) else "ARTIFACT_INTEGRITY_ERROR"
            updated.update(status="failed", terminal_reason=code, final_manifest_ref=None,
                last_error=ErrorSummary(code=code, node="publish", retriable=False,
                                       message="Publication could not verify current artifacts"))
        except Exception:
            updated.update(status="failed", terminal_reason="PDF_LAYOUT_FAILED", final_manifest_ref=None,
                last_error=ErrorSummary(code="PDF_LAYOUT_FAILED", node="publish", retriable=False,
                                       message="PDF publication failed"))
        if state["status"] != "completed" and state["step_count"] < limits.max_graph_steps:
            updated["step_count"] += 1
        updated["node_status"]["publish"] = updated["status"]
        if updated["status"] != "completed":
            updated = await make_abort_node(store=store, now=now, limits=limits)(updated)
        return validate_state(updated, max_graph_steps=limits.max_graph_steps)

    return publish


def make_abort_node(*, store: ArtifactStore, now: Callable[[], datetime] = utc_now, limits: RunConfig | None = None):
    async def abort(state: SupervisorState) -> SupervisorState:
        updated = copy_state(state)
        if updated["status"] == "running":
            updated["status"] = "failed"
            updated["terminal_reason"] = "ARTIFACT_INTEGRITY_ERROR"
        updated["final_manifest_ref"] = None
        diagnostic = {
            "run_id": state["run_id"], "status": updated["status"],
            "terminal_reason": updated["terminal_reason"], "step_count": state["step_count"],
            "draft_report_ref": _data(state["draft_report_ref"]),
            "eval_ref": _data(state["eval_ref"]), "gate_ref": _data(state["gate_ref"]),
            "last_error": _data(state["last_error"]), "ts": now().isoformat(),
        }
        # Terminal diagnostics may outlive the run deadline, but never tool timeout.
        io = bounded_store(store, state, limits=limits, now=now, finalizer=True)
        try:
            await io.save(diagnostic, key="diagnostics.json")
            updated["node_status"]["abort"] = "diagnostics_saved"
        except Exception:
            updated["node_status"]["abort"] = "diagnostics_failed"
        return validate_state(updated)
    return abort
