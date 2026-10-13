"""AT04/AT16 through real Sooyoung workers, wrappers, Supervisor and graph.

ResearchTools, LLMClient, evidence assessor, ArtifactStore and report/PDF
services are deterministic injected adapters. Technology and eligible-target
seed data come from the common Supervisor test helper. This suite does not
claim live search, model quality, actual PDF rendering or CLI registration.
"""
import asyncio
import json
from pathlib import Path
import sys

import pytest
from langgraph.checkpoint.memory import InMemorySaver

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "unit/v2/dongwook"))
from policy_fixtures import NOW, Resolver, evaluation, full_fixture, new_run

from main.agents.supervisor import contracts as c
from main.agents.supervisor.agent import Supervisor
from main.graph.supervisor_workflow import build_supervisor_graph


FIXTURES = Path(__file__).resolve().parents[3] / "fixtures/v2/sooyoung"
SCENARIO = json.loads((FIXTURES / "acceptance_research.json").read_text())


def finding_fixture():
    data = json.loads((FIXTURES / "research_findings.json").read_text())
    return ([c.EvalFinding.model_validate(value) for value in data["findings"]],
            [c.Gap.model_validate(value) for value in data["expected_gaps"]])


class EvidenceRegistry(Resolver):
    """Resolve only known synthetic evidence with its original source identity."""
    def __init__(self, bundle):
        self.sources = {source.source_id: source for source in bundle.sources}
        self.evidence = {item.evidence_id: item for item in bundle.evidence}

    def add(self, sources, evidence):
        self.sources.update({source.source_id: source for source in sources})
        self.evidence.update({item.evidence_id: item for item in evidence})

    async def resolve(self, evidence_ids):
        items = [self.evidence[item_id] for item_id in evidence_ids]
        source_ids = {item.source_id for item in items}
        return c.EvidenceBundle(
            sources=[self.sources[source_id] for source_id in sorted(source_ids)],
            evidence=items)


class EmptyTools:
    def __init__(self):
        self.calls = []

    async def search(self, query, **kwargs):
        self.calls.append(("search", query, kwargs))
        return c.SearchResult(hits=[], outcome="empty")

    async def fetch(self, hit):
        raise AssertionError("An empty search cannot request original content")

    async def retrieve(self, query, **kwargs):
        self.calls.append(("retrieve", query, kwargs))
        receipt = c.RetrievalReceipt(query_id=f"empty-{len(self.calls)}",
            query_text=query, queries_hash=c.canonical_hash({"query": query}),
            provider="pdf", corpus_version="1.0", index_version="1.0",
            filters={"company_id": kwargs["company_id"], "domain": kwargs["domain"].value},
            ranked_hits=[], outcome="empty", ts=NOW)
        return c.RetrievalResult(sources=[], evidence=[], receipt=receipt)


class ForbiddenLLM:
    async def generate(self, **kwargs):
        raise AssertionError("No admissible evidence can authorize model generation")


class FixtureResearchTools:
    """Current-attempt independent bodies; prior excluded sources stay excluded."""
    def __init__(self, perspective, store, registry):
        self.perspective, self.store, self.registry = perspective, store, registry
        self.attempt = 0
        self.calls = []
        self.sources, self.evidence = [], []

    async def prepare(self, attempt):
        self.attempt = attempt
        self.sources, self.evidence = [], []
        for index, text in enumerate(SCENARIO["facts"][self.perspective.value]):
            source_id = f"{self.perspective.value}-a{attempt}-s{index}"
            ref = await self.store.save_blob(text.encode(),
                key=f"acceptance-sources/{source_id}.txt", media_type="text/plain")
            source = c.Source(source_id=source_id,
                canonical_url=f"https://independent{index}.example/{source_id}",
                title=f"Independent Acme pilot {index}", publisher=f"Institution {index}",
                publisher_group=f"independent-{index}", source_type="institution",
                published_at=SCENARIO["published_at"], retrieved_at=NOW,
                corporate_controlled=False, sha256=ref.sha256, content_ref=ref)
            item = c.Evidence(evidence_id=f"{source_id}-e", source_id=source_id,
                company_id=SCENARIO["company_id"], chunk_id=f"{source_id}-chunk",
                quote=text, event_date=SCENARIO["published_at"])
            self.sources.append(source)
            self.evidence.append(item)
        self.registry.add(self.sources, self.evidence)

    async def search(self, query, **kwargs):
        self.calls.append(("search", query, kwargs))
        return c.SearchResult(hits=[], outcome="empty")

    async def fetch(self, hit):
        raise AssertionError("No web hits were provided by the fixture adapter")

    async def retrieve(self, query, **kwargs):
        assert kwargs["company_id"] == SCENARIO["company_id"]
        assert kwargs["top_k"] == 5
        self.calls.append(("retrieve", query, kwargs))
        receipt = c.RetrievalReceipt(
            query_id=f"{self.perspective.value}-a{self.attempt}-q{len(self.calls)}",
            query_text=query, queries_hash=c.canonical_hash({"query": query}),
            provider="pdf", corpus_version="1.0", index_version="1.0",
            filters={"company_id": kwargs["company_id"], "domain": kwargs["domain"].value},
            ranked_hits=[c.RankedHit(source_id=item.source_id, chunk_id=item.chunk_id,
                rank=index + 1, score=0.9) for index, item in enumerate(self.evidence)],
            outcome="success", ts=NOW)
        return c.RetrievalResult(sources=self.sources, evidence=self.evidence, receipt=receipt)


class GroundedFixtureLLM:
    def __init__(self):
        self.requests = []

    async def generate(self, *, model, system_prompt, payload, response_model):
        assert response_model is c.PerspectiveResult
        request = c.ResearchRequest.model_validate(payload["request"])
        self.requests.append(request)
        evidence = [c.Evidence.model_validate(value) for value in payload["evidence"]]
        assert len(evidence) >= 2
        by_source = {item.source_id: item for item in evidence}
        chosen = list(by_source.values())[:2]
        claims = [c.Claim(claim_id=f"{request.perspective.value}-a{request.attempt}-c{index}",
            perspective=request.perspective, text=item.quote, kind="fact",
            evidence_ids=[item.evidence_id], limitations=SCENARIO["limitations"])
            for index, item in enumerate(chosen)]
        return c.PerspectiveResult(task_id=request.task_id,
            perspective=request.perspective, attempt=request.attempt, claims=claims,
            evidence_ids=[item.evidence_id for item in evidence],
            answered_questions={question: [claims[index].claim_id]
                for index, question in enumerate(c.REQUIRED_QUESTIONS[request.perspective])},
            gaps=[], limitations=SCENARIO["limitations"], retrieval_receipts=[])


def dynamic_assessor(store):
    """An injected verifier stamps the actual generated claim/evidence hashes."""
    async def assess(request):
        evidence = {item.evidence_id: item for item in request.evidence}
        verdicts = []
        for perspective, result in request.perspective_results.items():
            refs = []
            for claim in result.claims:
                if claim.kind not in {"fact", "inference"}:
                    continue
                support = c.ClaimSupport(claim_id=claim.claim_id,
                    claim_hash=c.canonical_hash(claim),
                    evidence_hashes={item_id: c.canonical_hash(evidence[item_id])
                        for item_id in claim.evidence_ids}, support_status="supported",
                    reason="Synthetic fixture verifier confirms the deterministic fact.",
                    verifier_version="1.0")
                refs.append(await store.save(support,
                    key=f"acceptance-supports/{claim.claim_id}.json"))
            passed = len(refs) >= 2 and not result.gaps
            verdicts.append(c.SufficiencyVerdict(perspective=perspective,
                structural_pass=passed, semantic_pass=passed, gaps=result.gaps,
                evidence_ids=result.evidence_ids, claim_support_refs=refs,
                reason="Synthetic verifier checks actual returned claim bindings.",
                rubric_version="1.0"))
        return verdicts
    return assess


async def forbid_worker(request):
    raise AssertionError("This scenario must never dispatch this worker")


def graph(supervisor, workers, store, config, publish=forbid_worker):
    return build_supervisor_graph(supervisor=supervisor, workers=workers,
        publish_worker=publish, store=store, run_config=config,
        checkpointer=InMemorySaver(), now=lambda: NOW)


def required_workers():
    return {node: forbid_worker for node in
        ("eligibility", "writer", "evaluator", *(p.value for p in c.PERSPECTIVES))}


@pytest.mark.parametrize("condition", ["eligibility_unknown", "insufficient_market"])
def test_at04_actual_worker_exhausts_attempts_without_writer_or_report(condition):
    async def scenario():
        from main.agents.eligibility.agent import EligibilityAgent
        from main.agents.perspectives.market import MarketAgent

        limit = SCENARIO["at04_attempt_limit"]
        store, state = await new_run({"max_eligibility_attempts": limit,
            "max_research_attempts_per_view": limit})
        config = c.RunConfig.model_validate(await store.load(state["config_ref"]))
        tools = EmptyTools()
        calls = {"eligibility": 0, "market": 0, "writer": 0}
        workers = required_workers()

        if condition == "eligibility_unknown":
            registry = EvidenceRegistry(c.EvidenceBundle(sources=[], evidence=[]))
            real_worker = EligibilityAgent(tools=tools, llm=ForbiddenLLM(),
                domain=c.Domain.PHYSICAL_AI_ROBOTICS, model="research", store=store)
            async def eligibility(request):
                calls["eligibility"] += 1
                return await real_worker(request)
            workers["eligibility"] = eligibility
        else:
            _, _, bundle, _, _ = await full_fixture(store, state)
            registry = EvidenceRegistry(bundle)
            state["perspectives"][c.Perspective.MARKET] = c.PerspectiveSlot()
            state["research_attempts"][c.Perspective.MARKET] = 0
            real_worker = MarketAgent(tools=tools, llm=ForbiddenLLM(),
                domain=c.Domain.PHYSICAL_AI_ROBOTICS, model="research", store=store)
            async def market(request):
                calls["market"] += 1
                return await real_worker(request)
            workers["market"] = market

        async def writer(request):
            calls["writer"] += 1
            raise AssertionError("AT04 must block writer before invocation")
        workers["writer"] = writer
        supervisor = Supervisor(store, registry, dynamic_assessor(store), now=lambda: NOW)
        result = await graph(supervisor, workers, store, config).ainvoke(state,
            {"configurable": {"thread_id": state["thread_id"]}})
        assert result["status"] == "incomplete", result.get("last_error")
        expected_reason = "ELIGIBILITY_UNKNOWN" if condition == "eligibility_unknown" else "INSUFFICIENT_EVIDENCE"
        assert result["terminal_reason"] == expected_reason
        assert calls["writer"] == 0
        assert calls["eligibility" if condition == "eligibility_unknown" else "market"] == limit
        if condition == "eligibility_unknown":
            assert result["eligibility_attempts"] == limit
        else:
            assert result["research_attempts"][c.Perspective.MARKET] == limit
        assert not result["evidence_gate_pass"]
        assert result["draft_report_ref"] is None
        assert result["final_manifest_ref"] is None
        assert "manifest.json" not in store.payloads
        assert store.payloads["diagnostics.json"]["status"] == "incomplete"
    asyncio.run(scenario())


def test_at16_actual_market_and_domain_workers_preserve_both_findings_in_one_cycle():
    async def scenario():
        from main.agents.perspectives.domain import DomainAgent
        from main.agents.perspectives.market import MarketAgent
        from main.agents.perspectives.stakeholders import StakeholdersAgent

        store, state = await new_run()
        _, _, bundle, _, _ = await full_fixture(store, state)
        registry = EvidenceRegistry(bundle)
        config = c.RunConfig.model_validate(await store.load(state["config_ref"]))
        findings, expected_gaps = finding_fixture()
        assert [finding.target_perspective for finding in findings] == [gap.perspective for gap in expected_gaps]
        llm = GroundedFixtureLLM()
        workers = required_workers()
        classes = {c.Perspective.MARKET: MarketAgent,
            c.Perspective.STAKEHOLDERS: StakeholdersAgent,
            c.Perspective.DOMAIN_APPLICATION: DomainAgent}
        worker_requests = []
        for perspective, worker_class in classes.items():
            state["perspectives"][perspective] = c.PerspectiveSlot()
            state["research_attempts"][perspective] = 0
            tools = FixtureResearchTools(perspective, store, registry)
            real_worker = worker_class(tools=tools, llm=llm,
                domain=c.Domain.PHYSICAL_AI_ROBOTICS, model="research", store=store)
            async def research(request, tools=tools, real_worker=real_worker):
                worker_requests.append(request)
                await tools.prepare(request.attempt)
                return await real_worker(request)
            workers[perspective.value] = research

        writer_requests, evaluator_requests, snapshots = [], [], []
        async def writer(request):
            writer_requests.append(request)
            sections, citations, claim_ids = [], [], []
            for section in c.SECTION_ORDER:
                paragraphs = []
                result = request.perspective_results.get(section.value)
                for claim in result.claims if result is not None else []:
                    paragraph_id = f"r{request.report_revision}-{claim.claim_id}"
                    paragraphs.append(c.ReportParagraph(paragraph_id=paragraph_id,
                        text=claim.text, kind=claim.kind, claim_ids=[claim.claim_id],
                        evidence_ids=claim.evidence_ids))
                    claim_ids.append(claim.claim_id)
                    for item_id in claim.evidence_ids:
                        item = next(e for e in request.evidence if e.evidence_id == item_id)
                        citations.append(c.Citation(source_id=item.source_id,
                            evidence_id=item_id, paragraph_id=paragraph_id))
                sections.append(c.ReportSection(section_id=section, title=section.value,
                    paragraphs=paragraphs))
            return c.DraftReport(revision=request.report_revision,
                evidence_epoch=request.evidence_epoch, sections=sections,
                claim_ids=claim_ids, citations=citations)

        async def judge(request):
            evaluator_requests.append(request)
            return evaluation(request.report, passing=len(evaluator_requests) > 1,
                findings=findings if len(evaluator_requests) == 1 else [])

        class ObservedSupervisor(Supervisor):
            async def step(self, incoming, **kwargs):
                result = await super().step(incoming, **kwargs)
                snapshots.append(result)
                return result

        workers.update(writer=writer, evaluator=judge)
        supervisor = ObservedSupervisor(store, registry, dynamic_assessor(store), now=lambda: NOW)
        runtime = graph(supervisor, workers, store, config)
        # Observe serial scheduling through the actual graph; stop when publish
        # is reserved so this test does not claim a real PDF service is present.
        final = None
        async for event in runtime.astream(state,
                {"configurable": {"thread_id": state["thread_id"]}}, stream_mode="values"):
            if event.get("next_action") == c.NextAction.PUBLISH:
                final = event
                break
        assert final is not None, snapshots[-1].get("last_error") if snapshots else "No Supervisor result"
        assert final["repair_cycles"] == 1
        assert final["report_revision"] == 2
        assert len(writer_requests) == len(evaluator_requests) == 2
        assert final["evidence_gate_pass"]
        plan = c.RepairPlan.model_validate(await store.load(final["repair_plan_ref"]))
        assert tuple(plan.findings) == tuple(findings)
        assert set(plan.research_targets) == {c.Perspective.MARKET, c.Perspective.DOMAIN_APPLICATION}
        assert len(plan.gap_ids) == 2
        assert set(plan.gap_ids) <= set(final["resolved_gap_ids"])
        expected_by_target = {gap.perspective: gap for gap in expected_gaps}
        for gap in supervisor.plan_gaps(plan):
            expected = expected_by_target[gap.perspective]
            assert gap.question == expected.question
            assert gap.kind == expected.kind
            assert gap.query_hints == expected.query_hints

        repair_requests = [request for request in worker_requests if request.attempt == 2]
        assert len(worker_requests) == len(llm.requests) == 5
        assert {request.perspective for request in worker_requests if request.attempt == 1} == set(classes)
        assert {request.perspective for request in repair_requests} == set(plan.research_targets)
        assert len(repair_requests) == 2
        for request in repair_requests:
            gap_id = plan.gap_ids[next(index for index, finding in enumerate(plan.findings)
                if finding.target_perspective == request.perspective)]
            assert gap_id in request.gap_ids
            assert expected_by_target[request.perspective].question in request.query_hints
            assert request.prior_result_ref is not None
            assert request.excluded_source_ids

        # First successful repair acceptance invalidates eval, while the second
        # target and its exact immutable gap remain queued in the same cycle.
        first_target = repair_requests[0].perspective
        second_target = repair_requests[1].perspective
        intermediate = next(snapshot for snapshot in snapshots
            if snapshot["repair_cycles"] == 1
            and snapshot["perspectives"][first_target].status == "sufficient"
            and snapshot["active_task"] is not None
            and snapshot["active_task"].perspective == second_target)
        assert intermediate["eval_ref"] is None
        assert not intermediate["evidence_gate_pass"]
        assert intermediate["repair_plan_ref"] == final["repair_plan_ref"]
        second_gap = plan.gap_ids[next(index for index, finding in enumerate(plan.findings)
            if finding.target_perspective == second_target)]
        assert second_gap in intermediate["perspectives"][second_target].gap_ids
        assert second_gap not in intermediate["resolved_gap_ids"]
        assert all(snapshot["repair_cycles"] <= 1 for snapshot in snapshots)
        current_eval = c.EvalVerdict.model_validate(await store.load(final["eval_ref"]))
        assert current_eval.overall_pass
        assert current_eval.report_revision == 2
        assert current_eval.evidence_epoch == final["evidence_epoch"]
        for request in writer_requests:
            assert request.gate.evidence_epoch == request.evidence_epoch
        assert writer_requests[1].evidence_epoch == writer_requests[0].evidence_epoch + 2
        assert [request.attempt for request in worker_requests if request.perspective == c.Perspective.STAKEHOLDERS] == [1]
    asyncio.run(scenario())
