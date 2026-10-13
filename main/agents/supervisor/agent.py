"""Single-writer Supervisor: validate results, reserve work, and record decisions."""
from __future__ import annotations

import asyncio
from copy import copy
from datetime import datetime, timezone
from uuid import uuid5, UUID

from main.agents.supervisor import contracts as c
from main.agents.supervisor.policy import choose_research_target, gate_matches, report_matches, evaluation_matches, structural_gaps, terminal_status
from main.graph.supervisor_state import SupervisorState, StateIntegrityError, copy_state, validate_state, state_digest, stable_task_id


class ServiceTimeout(TimeoutError):
    def __init__(self, deadline_exhausted):
        super().__init__("Supervisor dependency exceeded the allowed timeout")
        self.deadline_exhausted = deadline_exhausted


async def bounded_call(state, timeout, factory, *, now=None, finalizer=False, completion_recovery=False):
    """Defer dependency creation until a finite tool/run budget is available.

    Diagnostic finalizers and read-only committed-result reconciliation may
    ignore the run deadline, while retaining a finite tool timeout. A provider
    timeout before our timer expires remains a dependency failure.
    """
    now = now or (lambda: datetime.now(timezone.utc))
    remaining = (state["deadline_at"] - now()).total_seconds()
    deadline_exempt = finalizer or completion_recovery
    if not deadline_exempt and remaining <= 0:
        raise ServiceTimeout(True)
    allowed = float(timeout) if deadline_exempt else min(float(timeout), remaining)
    timer = asyncio.timeout(allowed)
    try:
        async with timer:
            return await factory()
    except ServiceTimeout:
        raise
    except TimeoutError as exc:
        exhausted = not deadline_exempt and (now() >= state["deadline_at"] or (timer.expired() and remaining <= float(timeout)))
        raise ServiceTimeout(exhausted) from exc


class BoundedArtifactStore:
    """Timeout facade only; persistence and idempotency remain adapter-owned."""
    def __init__(self, store, state, *, limits=None, now=None, finalizer=False, completion_recovery=False):
        self.raw = store.raw if isinstance(store, BoundedArtifactStore) else store
        self.state = state
        self.limits = limits or c.RunConfig()
        self.now = now
        self.finalizer = finalizer
        self.completion_recovery = completion_recovery

    def __getattr__(self, name):
        if self.completion_recovery and name not in {"load", "verify", "find_manifest"}:
            raise StateIntegrityError("Completion recovery permits only committed-artifact reads")
        operation = getattr(self.raw, name)
        async def call(*args, **kwargs):
            return await bounded_call(self.state, self.limits.tool_timeout_seconds,
                lambda: operation(*args, **kwargs), now=self.now, finalizer=self.finalizer,
                completion_recovery=self.completion_recovery)
        return call


def bounded_store(store, state, *, limits=None, now=None, finalizer=False, completion_recovery=False):
    """Scope I/O budgets; completion recovery is read-only and tool-bounded."""
    if isinstance(store, BoundedArtifactStore) and not finalizer and not completion_recovery:
        if limits is not None:
            store.limits = limits
        return store
    return BoundedArtifactStore(store, state, limits=limits, now=now, finalizer=finalizer,
                                completion_recovery=completion_recovery)


async def load_model(store, ref, model):
    return model.model_validate(await store.load(ref))


async def current_report(state, store):
    store = bounded_store(store, state)
    if state["draft_report_ref"] is None:
        return None
    report = await load_model(store, state["draft_report_ref"], c.DraftReport)
    return report if report_matches(state, report) else None


async def current_evaluation(state, store, report=None):
    store = bounded_store(store, state)
    report = report or await current_report(state, store)
    if report is None or state["eval_ref"] is None:
        return None
    evaluation = await load_model(store, state["eval_ref"], c.EvalVerdict)
    return evaluation if evaluation_matches(state, report, evaluation) else None


async def validate_current_gate(state, store, config=None, *, now=None):
    """Load and verify certificate against actual artifacts, never a bool alone."""
    if not state["evidence_gate_pass"] or state["gate_ref"] is None or state["eligibility_ref"] is None:
        return False
    store = bounded_store(store, state, limits=config, now=now)
    config = config or await load_model(store, state["config_ref"], c.RunConfig)
    store.limits = config
    eligibility = await load_model(store, state["eligibility_ref"], c.EligibilityResult)
    gate = await load_model(store, state["gate_ref"], c.GateCertificate)
    if not gate_matches(state, gate, eligibility.result_hash, config.rubric_version):
        return False
    results, verdicts, supports = {}, {}, {}
    for p, slot in state["perspectives"].items():
        if slot.latest_result_ref is None or slot.sufficiency_ref is None:
            return False
        result = await load_model(store, slot.latest_result_ref, c.PerspectiveResult)
        verdict = await load_model(store, slot.sufficiency_ref, c.SufficiencyVerdict)
        if result.perspective != p or result.result_hash != slot.result_hash or not verdict.structural_pass or not verdict.semantic_pass or verdict.rubric_version != config.rubric_version:
            return False
        results[p], verdicts[p] = result, verdict
        for ref in verdict.claim_support_refs:
            support = await load_model(store, ref, c.ClaimSupport)
            if support.claim_id in supports and supports[support.claim_id] != support:
                raise ValueError("Conflicting current ClaimSupport records")
            supports[support.claim_id] = support
    reason = await store.load(gate.reason_ref)
    if "evidence_bundle" not in reason:
        return False
    bundle = c.EvidenceBundle.model_validate(reason["evidence_bundle"])
    for source in bundle.sources:
        await store.verify(source.content_ref)
    run_input = await load_model(store, state["input_ref"], c.RunInput)
    company = await store.load(state["company_ref"])
    if structural_gaps(company["company_id"], run_input.as_of_date, results, verdicts, bundle, list(supports.values()), eligibility):
        return False
    if state["repair_plan_ref"]:
        plan = await load_model(store, state["repair_plan_ref"], c.RepairPlan)
        research_gap_ids = {plan.gap_ids[index] for index, finding in enumerate(plan.findings) if finding.repair_kind == c.RepairKind.RESEARCH}
        if not research_gap_ids <= set(state["resolved_gap_ids"]):
            return False
    return True


async def gate_approved_bundle(state, store):
    """Return the immutable source/evidence snapshot named by the current gate."""
    store = bounded_store(store, state)
    gate = await load_model(store, state["gate_ref"], c.GateCertificate)
    reason = await store.load(gate.reason_ref)
    return c.EvidenceBundle.model_validate(reason["evidence_bundle"])


def approved_claims(eligibility, results):
    """Deduplicate approved target and perspective claims by validated identity."""
    return c.deduplicate_claims([*eligibility.claims, *(cl for result in results.values() for cl in result.claims)])


async def validate_writer_inputs(state, store, request):
    """The gate authorizes its stored inputs, even if a task hash was replaced."""
    store = bounded_store(store, state)
    bundle = await gate_approved_bundle(state, store)
    if c.canonical_hash(bundle) != c.canonical_hash(c.EvidenceBundle(sources=request.sources, evidence=request.evidence)):
        return False
    gate = await load_model(store, state["gate_ref"], c.GateCertificate)
    eligibility = await load_model(store, state["eligibility_ref"], c.EligibilityResult)
    run_input = await load_model(store, state["input_ref"], c.RunInput)
    company = await store.load(state["company_ref"])
    if (request.gate != gate or request.eligibility != eligibility or
        request.company_id != company["company_id"] or request.as_of_date != run_input.as_of_date or
        set(request.perspective_results) != set(state["perspectives"])):
        return False
    supports = {}
    for p, slot in state["perspectives"].items():
        result = await load_model(store, slot.latest_result_ref, c.PerspectiveResult)
        if request.perspective_results[p] != result:
            return False
        verdict = await load_model(store, slot.sufficiency_ref, c.SufficiencyVerdict)
        for ref in verdict.claim_support_refs:
            support = await load_model(store, ref, c.ClaimSupport)
            supports[support.claim_id] = support
    return len(request.claim_supports) == len(supports) and {s.claim_id: s for s in request.claim_supports} == supports


async def validate_evaluator_inputs(state, store, request):
    """Bind evaluation to the latest draft and the gate-approved claim snapshot."""
    store = bounded_store(store, state)
    bundle = await gate_approved_bundle(state, store)
    if c.canonical_hash(bundle) != c.canonical_hash(c.EvidenceBundle(sources=request.sources, evidence=request.evidence)):
        return False
    eligibility = await load_model(store, state["eligibility_ref"], c.EligibilityResult)
    results = {p: await load_model(store, slot.latest_result_ref, c.PerspectiveResult)
               for p, slot in state["perspectives"].items()}
    claims = approved_claims(eligibility, results)
    run_input = await load_model(store, state["input_ref"], c.RunInput)
    return (request.report == await current_report(state, store) and
            request.as_of_date == run_input.as_of_date and len(request.claims) == len(claims) and
            {cl.claim_id: cl for cl in request.claims} == {cl.claim_id: cl for cl in claims})


class Supervisor:
    def __init__(self, store: c.ArtifactStore, evidence_resolver: c.EvidenceResolver,
                 assess_evidence, now=None, decision_sink=None, limits=None):
        self.store = store
        self.evidence_resolver = evidence_resolver
        self.assess_evidence = assess_evidence
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.decision_sink = decision_sink
        self.limits = limits or c.RunConfig()

    async def save(self, payload, key):
        data = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload
        return await self.store.save(data, key=key)

    async def bounded(self, state, timeout, factory):
        return await bounded_call(state, timeout, factory, now=self.now)

    def stop(self, state, reason, *, failed=False, message=None):
        state["status"] = "failed" if failed else "incomplete"
        state["terminal_reason"] = str(reason.value if hasattr(reason, "value") else reason)
        state["next_action"] = c.NextAction.ABORT
        state["active_task"] = None
        state["pending_result"] = None
        state["final_manifest_ref"] = None
        state["evidence_gate_pass"] = False
        state["gate_ref"] = None
        if failed:
            state["last_error"] = c.ErrorSummary(code=reason, node="supervisor", retriable=False, message=(message or state["terminal_reason"])[:2000])

    async def step(self, state: SupervisorState, *, limits=None) -> SupervisorState:
        scoped = copy(self)
        scoped.store = bounded_store(self.store, state, limits=limits or self.limits, now=self.now)
        result = await scoped._step(state)
        try:
            return validate_state(result)
        except StateIntegrityError as exc:
            if exc.code != "STATE_TOO_LARGE":
                raise
            validate_state(state)
            result = copy_state(state)
            scoped.stop(result, "STATE_TOO_LARGE", failed=True, message=str(exc))
            return validate_state(result)

    async def _step(self, state: SupervisorState) -> SupervisorState:
        updated = copy_state(state)
        try:
            config = await load_model(self.store, updated["config_ref"], c.RunConfig)
            self.store.limits = config
            validate_state(updated, max_graph_steps=config.max_graph_steps)
            if updated["status"] != "running":
                return updated
            fresh_perspective = None
            if updated["pending_result"] is not None:
                if updated["active_task"] is not None and updated["pending_result"].outcome == "success":
                    fresh_perspective = updated["active_task"].perspective
                await self.accept(updated, config)
            if updated["status"] != "running":
                return await self.decide(updated, state, config, updated["terminal_reason"])
            if updated["active_task"] is not None:
                updated["next_action"] = c.NextAction(updated["active_task"].node.value)
                return updated
            # One new Supervisor decision and the next worker each consume a step.
            if self.now() >= updated["deadline_at"] or updated["step_count"] + 2 > config.max_graph_steps:
                self.stop(updated, c.TerminalReason.BUDGET_EXHAUSTED)
                return await self.decide(updated, state, config, "No budget remains for a new work node", count=False)
            run_input = await load_model(self.store, updated["input_ref"], c.RunInput)
            company = await self.store.load(updated["company_ref"])
            if updated["eligibility_status"] == "ineligible":
                self.stop(updated, c.TerminalReason.INELIGIBLE_TARGET)
                reason = "Target violates eligibility conditions"
            elif updated["eligibility_status"] != "eligible":
                reason = await self.confirm_eligibility(updated, run_input, company, config)
            else:
                if all(slot.latest_result_ref is not None for slot in updated["perspectives"].values()) and not await validate_current_gate(updated, self.store, config):
                    eligibility_gaps = await self.refresh_evidence(updated, run_input, company, config, fresh_perspective)
                    if updated["status"] != "running":
                        return await self.decide(updated, state, config, updated["terminal_reason"])
                    if updated["eligibility_status"] != "eligible":
                        reason = await self.confirm_eligibility(updated, run_input, company, config, eligibility_gaps)
                        return await self.decide(updated, state, config, reason)
                target = choose_research_target(updated["perspectives"], await self.load_gaps(updated))
                if target is not None:
                    reason = await self.research(updated, target, run_input, company, config)
                else:
                    reason = await self.approved_flow(updated, run_input, company, config)
            return await self.decide(updated, state, config, reason)
        except ServiceTimeout as exc:
            self.stop(updated, c.TerminalReason.BUDGET_EXHAUSTED if exc.deadline_exhausted else c.TerminalReason.MODEL_OUTPUT_INVALID, failed=not exc.deadline_exhausted, message=str(exc))
            return updated
        except StateIntegrityError as exc:
            if exc.code == "STATE_TOO_LARGE":
                # The incoming checkpoint must itself be valid. Do not truncate
                # accepted gap IDs or attempt to repair an invalid raw input.
                validate_state(state)
                updated = copy_state(state)
            self.stop(updated, exc.code, failed=True, message=str(exc))
            return validate_state(updated)
        except Exception as exc:
            self.stop(updated, c.TerminalReason.ARTIFACT_INTEGRITY_ERROR, failed=True, message=str(exc))
            return updated

    async def decide(self, state, before, config, reason, *, count=True):
        if count:
            state["step_count"] += 1
        decision_id = str(uuid5(UUID(state["run_id"]), f"decision:{state['step_count']}:{state['next_action'].value}"))
        gaps = sorted({g for slot in state["perspectives"].values() for g in slot.gap_ids})
        decision = c.Decision(decision_id=decision_id, run_id=state["run_id"], trace_id=state["trace_id"], step=state["step_count"], next_action=state["next_action"], reason=reason, gap_ids=gaps, input_state_hash=state_digest(before), policy_version=config.policy_version, ts=self.now())
        await self.save(decision, f"decisions/{decision_id}.json")
        if self.decision_sink:
            await self.bounded(state, config.tool_timeout_seconds, lambda: self.decision_sink(decision))
        state["last_decision_id"] = decision_id
        return validate_state(state, max_graph_steps=config.max_graph_steps)

    async def reserve(self, state, node, request, attempt, perspective=None):
        payload_ref = await self.save(request, f"tasks/{request.task_id}/request.json")
        state["active_task"] = c.TaskEnvelope(task_id=request.task_id, node=node, perspective=perspective, logical_attempt=attempt, report_revision=state["report_revision"] if node in {"writer", "evaluator"} else None, evidence_epoch=state["evidence_epoch"], payload_ref=payload_ref, input_hash=c.canonical_hash(request.model_dump(mode="json")), dispatched_at=self.now())
        state["next_action"] = c.NextAction(node)
        state["transient_retries"] = 0
        state["node_status"][node] = "reserved"

    async def confirm_eligibility(self, state, run_input, company, config, gaps=None):
        """Reserve one target retry, preserving the dated-evidence gaps that caused it."""
        if state["eligibility_attempts"] >= config.max_eligibility_attempts:
            self.stop(state, c.TerminalReason.ELIGIBILITY_UNKNOWN)
            return "Eligibility remains unknown at the attempt limit"
        if gaps is None:
            result = await load_model(self.store, state["eligibility_ref"], c.EligibilityResult) if state["eligibility_ref"] else None
            gaps = list(result.gaps) if result else []
        attempt = state["eligibility_attempts"] + 1
        state["eligibility_attempts"] = attempt
        task_id = stable_task_id(state["run_id"], "eligibility", None, attempt)
        request = c.EligibilityRequest(task_id=task_id, company_id=company["company_id"], company_name=run_input.company_name,
            official_url=run_input.official_url, as_of_date=run_input.as_of_date, attempt=attempt,
            gap_ids=[g.gap_id for g in gaps], instructions=[f"Resolve {g.kind.value}: {g.question}" for g in gaps])
        await self.reserve(state, "eligibility", request, attempt)
        return "Confirm target conditions with dated sources"

    async def load_gaps(self, state):
        gaps = []
        ids = {g for slot in state["perspectives"].values() for g in slot.gap_ids}
        for slot in state["perspectives"].values():
            if not slot.gap_ids:
                continue
            for ref, model in ((slot.latest_result_ref, c.PerspectiveResult), (slot.sufficiency_ref, c.SufficiencyVerdict)):
                if ref:
                    payload = await load_model(self.store, ref, model)
                    gaps.extend(g for g in payload.gaps if g.gap_id in ids)
        if state["repair_plan_ref"]:
            plan = await load_model(self.store, state["repair_plan_ref"], c.RepairPlan)
            gaps.extend(g for g in self.plan_gaps(plan) if g.gap_id in ids)
        return list({g.gap_id: g for g in gaps}.values())

    def plan_gaps(self, plan):
        return [c.Gap(gap_id=plan.gap_ids[index], perspective=finding.target_perspective or "report", kind="missing_evidence" if finding.repair_kind == c.RepairKind.RESEARCH else "report_expression", question=finding.reason, severity=finding.severity, evidence_ids=list(finding.evidence_ids), query_hints=[finding.reason]) for index, finding in enumerate(plan.findings)]

    async def research(self, state, target, run_input, company, config):
        if state["research_attempts"][target] >= config.max_research_attempts_per_view:
            self.stop(state, c.TerminalReason.INSUFFICIENT_EVIDENCE)
            return f"{target} remains insufficient after the last allowed attempt"
        slot = state["perspectives"][target]
        attempt = state["research_attempts"][target] + 1
        state["research_attempts"][target] = attempt
        state["perspectives"][target] = slot.model_copy(update={"attempt": attempt})
        gaps = [g for g in await self.load_gaps(state) if g.perspective == target]
        prior_evidence = []
        excluded = []
        if slot.latest_result_ref:
            prior = await load_model(self.store, slot.latest_result_ref, c.PerspectiveResult)
            prior_evidence = list(prior.evidence_ids)
            if prior_evidence:
                prior_bundle = await self.bounded(state, config.tool_timeout_seconds, lambda: self.evidence_resolver.resolve(prior_evidence))
                excluded = sorted({e.source_id for e in prior_bundle.evidence})
        task_id = stable_task_id(state["run_id"], target, target, attempt)
        instructions = [f"Resolve {g.kind.value}: {g.question}" for g in gaps] or ["Collect evidence for both fixed questions; include limitations and independent publishers"]
        instructions += [f"Existing evidence IDs: {', '.join(prior_evidence) or 'none'}", "Required material: original full text, independent publisher evidence; technology must use retrieved PDF pages"]
        request = c.ResearchRequest(task_id=task_id, company_id=company["company_id"], perspective=target, as_of_date=run_input.as_of_date, prior_result_ref=slot.latest_result_ref, gap_ids=list(slot.gap_ids), instructions=instructions, query_hints=list(dict.fromkeys(h for g in gaps for h in g.query_hints)), excluded_source_ids=excluded, attempt=attempt, corpus_version=config.corpus_version)
        await self.reserve(state, target, request, attempt, target)
        return f"Research {target}: resolve its outstanding gaps"

    async def accept(self, state, config):
        result, task = state["pending_result"], state["active_task"]
        if result.task_id in state["accepted_task_ids"]:
            if task is not None and task.task_id == result.task_id:
                state["active_task"] = None
            state["pending_result"] = None
            return
        if task is None or task.task_id != result.task_id:
            raise ValueError("pending result task_id does not match active_task")
        if result.outcome == "transient_error" and result.error.retriable:
            if self.now() >= state["deadline_at"]:
                self.stop(state, c.TerminalReason.BUDGET_EXHAUSTED)
            elif state["transient_retries"] < config.max_transient_retries:
                state["transient_retries"] += 1
                state["last_error"] = result.error
                state["pending_result"] = None
                return
            else:
                self.stop(state, result.error.code, failed=True, message=result.error.message)
        elif result.outcome in {"permanent_error", "transient_error"}:
            error = result.error
            self.stop(state, error.code, failed=terminal_status(error.code) == "failed", message=error.message)
            state["last_error"] = error
        elif result.outcome == "empty":
            if task.perspective is not None:
                empty = await load_model(self.store, result.result_ref, c.PerspectiveResult)
                if empty.claims:
                    raise ValueError("Empty outcome contains nonempty research claims")
                await self.accept_success(state, task, result.result_ref)
                slot = state["perspectives"][task.perspective]
                gap_id = str(uuid5(UUID(state["run_id"]), f"empty:{task.task_id}"))
                gap = c.Gap(gap_id=gap_id, perspective=task.perspective, kind="missing_evidence", question="No usable evidence returned; use a different query or source", severity="critical", query_hints=["Seek original independent reports"])
                await self.save(gap, f"gaps/{gap_id}.json")
                verdict = c.SufficiencyVerdict(perspective=task.perspective, structural_pass=False, semantic_pass=False, gaps=[gap], evidence_ids=[], claim_support_refs=[], reason="Empty research result", rubric_version=config.rubric_version)
                ref = await self.save(verdict, f"tasks/{task.task_id}/empty-verdict.json")
                state["perspectives"][task.perspective] = slot.model_copy(update={"status": "insufficient", "gap_ids": list(dict.fromkeys([*slot.gap_ids, gap_id])), "sufficiency_ref": ref})
                self.invalidate_gate(state)
            elif task.node == "eligibility":
                empty = await load_model(self.store, result.result_ref, c.EligibilityResult)
                if empty.verdict != "unknown":
                    raise ValueError("Empty eligibility outcome must be unknown")
                await self.accept_success(state, task, result.result_ref)
                state["eligibility_status"] = "unknown"
                self.invalidate_gate(state)
            else:
                self.stop(state, c.TerminalReason.MODEL_OUTPUT_INVALID, failed=True, message="Writer/evaluator returned empty output")
        else:
            await self.accept_success(state, task, result.result_ref)
        state["accepted_task_ids"] = [*state["accepted_task_ids"], result.task_id]
        state["active_task"] = None
        state["pending_result"] = None
        state["transient_retries"] = 0

    def invalidate_gate(self, state):
        state["evidence_gate_pass"] = False
        state["gate_ref"] = None
        state["eval_ref"] = None

    async def accept_success(self, state, task, ref):
        if task.node == "eligibility":
            eligibility = await load_model(self.store, ref, c.EligibilityResult)
            if eligibility.task_id != task.task_id:
                raise ValueError("Eligibility result task mismatch")
            state["eligibility_ref"] = ref
            state["eligibility_status"] = eligibility.verdict
            self.invalidate_gate(state)
        elif task.perspective is not None:
            result = await load_model(self.store, ref, c.PerspectiveResult)
            if (result.task_id, result.perspective, result.attempt) != (task.task_id, task.perspective, task.logical_attempt) or task.evidence_epoch != state["evidence_epoch"]:
                raise ValueError("Research result task/perspective/attempt/epoch mismatch")
            slot = state["perspectives"][task.perspective]
            state["perspectives"][task.perspective] = slot.model_copy(update={"status": "insufficient", "latest_result_ref": ref, "result_hash": result.result_hash, "sufficiency_ref": None, "gap_ids": list(dict.fromkeys([*slot.gap_ids, *(g.gap_id for g in result.gaps)])), "attempt": result.attempt})
            state["evidence_epoch"] += 1
            self.invalidate_gate(state)
        elif task.node == "writer":
            report = await load_model(self.store, ref, c.DraftReport)
            if (report.revision, report.evidence_epoch) != (task.report_revision, task.evidence_epoch) or not report_matches(state, report):
                raise ValueError("Writer result revision/epoch mismatch")
            state["draft_report_ref"] = ref
            state["eval_ref"] = None
        elif task.node == "evaluator":
            report = await current_report(state, self.store)
            evaluation = await load_model(self.store, ref, c.EvalVerdict)
            if report is None or not evaluation_matches(state, report, evaluation):
                raise ValueError("Evaluator result has stale report hash/revision/epoch")
            state["eval_ref"] = ref
        else:
            raise ValueError("Unsupported result node")

    async def approved_flow(self, state, run_input, company, config):
        if not await validate_current_gate(state, self.store, config):
            self.stop(state, c.TerminalReason.INSUFFICIENT_EVIDENCE)
            return "Evidence requires fresh structural and semantic approval"
        eligibility, results, bundle = await self.evidence_inputs(state, company["company_id"], config, approved=True)
        report = await current_report(state, self.store)
        if report is None:
            return await self.dispatch_writer(state, run_input, company, eligibility, results, bundle)
        evaluation = await current_evaluation(state, self.store, report)
        if evaluation is None:
            task_id = stable_task_id(state["run_id"], "evaluator", None, report.revision)
            request = c.EvaluatorRequest(task_id=task_id, report=report, claims=approved_claims(eligibility, results), evidence=bundle.evidence, sources=bundle.sources, as_of_date=run_input.as_of_date)
            await self.reserve(state, "evaluator", request, report.revision)
            return "Evaluate the latest report hash and evidence epoch"
        if evaluation.overall_pass:
            state["next_action"] = c.NextAction.PUBLISH
            return "Publish only the latest passing report and current gate"
        await self.start_repair(state, evaluation, config)
        if state["status"] != "running":
            return "Quality remains below the threshold after the allowed repair cycles"
        target = choose_research_target(state["perspectives"], await self.load_gaps(state))
        if target:
            return await self.research(state, target, run_input, company, config)
        return await self.dispatch_writer(state, run_input, company, eligibility, results, bundle)

    async def dispatch_writer(self, state, run_input, company, eligibility, results, bundle):
        state["report_revision"] += 1
        revision = state["report_revision"]
        task_id = stable_task_id(state["run_id"], "writer", None, revision)
        gate = await load_model(self.store, state["gate_ref"], c.GateCertificate)
        supports = await self.current_supports(state)
        instructions = []
        if state["repair_plan_ref"]:
            plan = await load_model(self.store, state["repair_plan_ref"], c.RepairPlan)
            instructions = list(plan.rewrite_instructions)
            instructions += [f.reason for f in plan.findings if f.repair_kind == c.RepairKind.RESEARCH]
        request = c.WriterRequest(task_id=task_id, company_id=company["company_id"], as_of_date=run_input.as_of_date, report_revision=revision, evidence_epoch=state["evidence_epoch"], eligibility=eligibility, perspective_results=results, gate=gate, sources=bundle.sources, evidence=bundle.evidence, claim_supports=supports, rewrite_instructions=instructions)
        state["eval_ref"] = None
        await self.reserve(state, "writer", request, revision)
        return "Write a new revision from the current approved evidence and repair instructions"

    async def start_repair(self, state, evaluation, config):
        if state["repair_plan_ref"]:
            prior = await load_model(self.store, state["repair_plan_ref"], c.RepairPlan)
            if prior.failed_report_revision == evaluation.report_revision:
                return
        if state["repair_cycles"] >= config.max_quality_repairs:
            self.stop(state, c.TerminalReason.QUALITY_EXHAUSTED)
            return
        cycle_id = str(uuid5(UUID(state["run_id"]), f"repair:{evaluation.report_revision}"))
        gap_ids = [str(uuid5(UUID(cycle_id), f"finding:{index}")) for index, finding in enumerate(evaluation.findings)]
        targets = sorted({f.target_perspective for f in evaluation.findings if f.repair_kind == c.RepairKind.RESEARCH}, key=lambda p: p.value)
        plan = c.RepairPlan(cycle_id=cycle_id, failed_report_revision=evaluation.report_revision, findings=tuple(evaluation.findings), gap_ids=tuple(gap_ids), research_targets=tuple(targets), rewrite_instructions=tuple(f.reason for f in evaluation.findings if f.repair_kind == c.RepairKind.REWRITE))
        state["repair_plan_ref"] = await self.save(plan, f"repairs/{cycle_id}.json")
        state["active_repair_cycle_id"] = cycle_id
        state["repair_cycles"] += 1
        state["resolved_gap_ids"] = []
        for p in targets:
            slot = state["perspectives"][p]
            gaps = [g.gap_id for g in self.plan_gaps(plan) if g.perspective == p]
            state["perspectives"][p] = slot.model_copy(update={"status": "insufficient", "gap_ids": list(dict.fromkeys([*slot.gap_ids, *gaps]))})
        if targets:
            self.invalidate_gate(state)

    async def evidence_inputs(self, state, company_id, config=None, *, approved=False):
        eligibility = await load_model(self.store, state["eligibility_ref"], c.EligibilityResult)
        results = {}
        for p, slot in state["perspectives"].items():
            if slot.latest_result_ref is not None:
                result = await load_model(self.store, slot.latest_result_ref, c.PerspectiveResult)
                if result.perspective != p or result.result_hash != slot.result_hash:
                    raise ValueError("Perspective artifact differs from State hash")
                results[p] = result
        ids = list(dict.fromkeys([i for result in results.values() for i in result.evidence_ids] + [i for cl in eligibility.claims for i in cl.evidence_ids]))
        config = config or await load_model(self.store, state["config_ref"], c.RunConfig)
        bundle = await gate_approved_bundle(state, self.store) if approved else c.EvidenceBundle.model_validate(await self.bounded(state, config.tool_timeout_seconds, lambda: self.evidence_resolver.resolve(ids)))
        c.validate_evidence_bundle(company_id=company_id, sources=bundle.sources, evidence=bundle.evidence, claims=approved_claims(eligibility, results))
        return eligibility, results, bundle

    async def current_supports(self, state):
        refs = {}
        for slot in state["perspectives"].values():
            if slot.sufficiency_ref is not None:
                verdict = await load_model(self.store, slot.sufficiency_ref, c.SufficiencyVerdict)
                refs.update({r.relative_path: r for r in verdict.claim_support_refs})
        return [await load_model(self.store, ref, c.ClaimSupport) for ref in refs.values()]

    async def refresh_evidence(self, state, run_input, company, config, fresh_perspective=None):
        eligibility, results, bundle = await self.evidence_inputs(state, company["company_id"], config)
        if len({s.source_id for s in bundle.sources}) > config.max_sources_per_run:
            self.stop(state, c.TerminalReason.BUDGET_EXHAUSTED)
            return
        request = c.SufficiencyRequest(eligibility=eligibility, perspective_results=results, sources=bundle.sources, evidence=bundle.evidence, as_of_date=run_input.as_of_date, evidence_epoch=state["evidence_epoch"])
        try:
            values = await self.bounded(state, config.llm_timeout_seconds, lambda: self.assess_evidence(request))
            verdicts = [c.SufficiencyVerdict.model_validate(value) for value in values]
            if len({v.perspective for v in verdicts}) != len(verdicts) or set(v.perspective for v in verdicts) != set(c.PERSPECTIVES):
                raise ValueError("Assessor must return exactly one verdict for each perspective")
        except ServiceTimeout:
            raise
        except Exception as exc:
            self.stop(state, c.TerminalReason.MODEL_OUTPUT_INVALID, failed=True, message=f"Sufficiency assessment failed: {exc}")
            return
        supports = []
        for verdict in verdicts:
            supports.extend([await load_model(self.store, ref, c.ClaimSupport) for ref in verdict.claim_support_refs])
        supports = list({support.claim_id: support for support in supports}.values())
        gaps = structural_gaps(company["company_id"], run_input.as_of_date, results, verdicts, bundle, supports, eligibility)
        plan = await load_model(self.store, state["repair_plan_ref"], c.RepairPlan) if state["repair_plan_ref"] else None
        if any(g.perspective == "eligibility" for g in gaps):
            state["eligibility_status"] = "unknown"
            self.invalidate_gate(state)
        for verdict in verdicts:
            p = verdict.perspective
            own_gaps = list({g.gap_id: g for g in [*verdict.gaps, *(g for g in gaps if g.perspective == p)]}.values())
            if plan:
                repair_gaps = [g for g in self.plan_gaps(plan) if g.perspective == p and g.gap_id not in state["resolved_gap_ids"]]
                if p == fresh_perspective and not any(g.severity == "critical" for g in own_gaps) and verdict.structural_pass and verdict.semantic_pass:
                    state["resolved_gap_ids"] = list(dict.fromkeys([*state["resolved_gap_ids"], *(g.gap_id for g in repair_gaps)]))
                else:
                    own_gaps = list({g.gap_id: g for g in [*own_gaps, *repair_gaps]}.values())
            adjusted = c.SufficiencyVerdict(**(verdict.model_dump(mode="json") | {"structural_pass": not any(g.severity == "critical" for g in own_gaps), "gaps": [g.model_dump(mode="json") for g in own_gaps], "result_hash": None}))
            ref = await self.save(adjusted, f"sufficiency/{state['evidence_epoch']}/{p.value}-{adjusted.result_hash}.json")
            evidence_ids = {i for claim in results[p].claims for i in claim.evidence_ids}
            source_ids = {e.source_id for e in bundle.evidence if e.evidence_id in evidence_ids}
            groups = {s.publisher_group for s in bundle.sources if s.source_id in source_ids}
            passed = adjusted.structural_pass and adjusted.semantic_pass
            state["perspectives"][p] = state["perspectives"][p].model_copy(update={"status": "sufficient" if passed else "insufficient", "sufficiency_ref": ref, "gap_ids": [g.gap_id for g in own_gaps], "source_count": len(source_ids), "publisher_group_count": len(groups), "answered_question_ids": list(c.REQUIRED_QUESTIONS[p]) if passed else []})
        if not gaps and all(slot.status == "sufficient" for slot in state["perspectives"].values()) and state["eligibility_status"] == "eligible":
            for source in bundle.sources:
                await self.store.verify(source.content_ref)
            reason_ref = await self.save({"reason": "All independent structural and semantic checks passed", "evidence_ids": sorted(e.evidence_id for e in bundle.evidence), "evidence_bundle": bundle.model_dump(mode="json")}, f"gates/{state['evidence_epoch']}/{c.canonical_hash(bundle)}/reason.json")
            gate = c.GateCertificate(evidence_epoch=state["evidence_epoch"], eligibility_hash=eligibility.result_hash, perspective_hashes={p: result.result_hash for p, result in results.items()}, rubric_version=config.rubric_version, passed=True, reason_ref=reason_ref)
            state["gate_ref"] = await self.save(gate, f"gates/{state['evidence_epoch']}/{c.canonical_hash(gate)}.json")
            state["evidence_gate_pass"] = True
        else:
            self.invalidate_gate(state)
        return [g for g in gaps if g.perspective == "eligibility"]
