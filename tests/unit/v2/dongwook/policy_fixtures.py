"""In-memory artifact boundary for real Supervisor/DTO tests only."""
import importlib
import hashlib
from datetime import datetime, timezone

import pytest

NOW = datetime(2026, 10, 13, 0, 0, tzinfo=timezone.utc)


def contracts():
    return importlib.import_module("main.agents.supervisor.contracts")


def supervisor_api():
    try:
        return importlib.import_module("main.agents.supervisor.agent")
    except ModuleNotFoundError:
        pytest.fail("Supervisor orchestration is not implemented")


class MemoryStore:
    def __init__(self):
        self.payloads = {}
        self.receipts = {}

    async def save(self, payload, key, *, media_type="application/json", schema_version="1.0"):
        c = contracts()
        data = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload
        digest = c.canonical_hash(data)
        ref = c.ArtifactRef(relative_path=key, sha256=digest, media_type=media_type, schema_version=schema_version)
        self.payloads[key] = data
        return ref

    async def load(self, ref):
        data = self.payloads[ref.relative_path]
        if isinstance(data, bytes):
            raise ValueError("Binary artifact cannot be loaded as JSON")
        if contracts().canonical_hash(data) != ref.sha256:
            raise ValueError("Artifact hash mismatch")
        return data

    async def verify(self, ref):
        data = self.payloads[ref.relative_path]
        digest = hashlib.sha256(data).hexdigest() if isinstance(data, bytes) else contracts().canonical_hash(data)
        if digest != ref.sha256:
            raise ValueError("Artifact hash mismatch")

    async def save_blob(self, data, key, media_type):
        self.payloads[key] = data
        return contracts().ArtifactRef(relative_path=key, sha256=hashlib.sha256(data).hexdigest(), media_type=media_type, schema_version="1.0")

    async def find_receipt(self, task_id, input_hash):
        return self.receipts.get((task_id, input_hash))

    async def save_receipt(self, task, result):
        self.receipts[task.task_id, task.input_hash] = result

    async def commit_manifest(self, manifest):
        return await self.save(manifest, "manifest.json")


class Resolver:
    def __init__(self, bundle=None):
        self.bundle = bundle

    async def resolve(self, evidence_ids):
        c = contracts()
        if self.bundle is None:
            return c.EvidenceBundle(sources=[], evidence=[])
        return c.EvidenceBundle(sources=self.bundle.sources, evidence=[e for e in self.bundle.evidence if e.evidence_id in evidence_ids])


async def new_run(config_changes=None):
    c = contracts()
    store = MemoryStore()
    config = c.RunConfig(**(dict(supervisor_model="supervisor", research_model="research", report_model="writer", judge_model="judge", prompt_hash="a" * 64) | (config_changes or {})))
    run_input = c.RunInput(company_name="Acme", official_url="https://example.com", domain="Physical AI / Robotics", as_of_date="2026-10-13", corpus_manifest=[])
    input_ref = await store.save(run_input, "input.json")
    config_ref = await store.save(config, "config.json")
    company_ref = await store.save({"company_id": "acme", "company_name": "Acme"}, "company.json")
    state_api = importlib.import_module("main.graph.supervisor_state")
    state = state_api.new_state(run_input, config, input_ref=input_ref, config_ref=config_ref, company_ref=company_ref, now=NOW)
    return store, state


async def eligible(store, state):
    c = contracts()
    result = c.EligibilityResult(task_id="eligible-seed", unlisted=True, funding_stage=True, exit_status=True, verdict="eligible", claims=[], gaps=[])
    state["eligibility_status"] = "eligible"
    state["eligibility_ref"] = await store.save(result, "eligibility.json")
    return result


async def no_verdicts(request):
    return []


async def full_fixture(store, state):
    """Eight supported claims, three publishers, original PDF and dated target."""
    c = contracts()
    sources = []
    for n in range(3):
        content_ref = await store.save({"text": "Confirmed conditions from original trial."}, key=f"sources/{n}.json")
        sources.append(c.Source(source_id=f"s{n}", canonical_url=f"https://publisher{n}.example/trial.pdf" if n == 0 else f"https://publisher{n}.example/trial", original_file_id="trial.pdf" if n == 0 else None, title="Trial", publisher=f"Publisher {n}", publisher_group=f"group-{n}", source_type="paper" if n == 0 else "institution", published_at="2026-01-01", retrieved_at=NOW, corporate_controlled=False, sha256=content_ref.sha256, content_ref=content_ref))
    evidence = []
    results = {}
    supports = {}
    verdicts = {}
    for index, p in enumerate(c.PERSPECTIVES):
        items = []
        claims = []
        for n in range(2):
            ev = c.Evidence(evidence_id=f"{p.value}-e{n}", source_id=f"s{(index + n) % 3}", company_id="acme", chunk_id=f"{p.value}-chunk{n}", page_number=2 if (index + n) % 3 == 0 else None, quote="Confirmed conditions from original trial.", event_date="2026-01-01")
            items.append(ev)
            evidence.append(ev)
            claim = c.Claim(claim_id=f"{p.value}-c{n}", perspective=p, text=f"Confirmed {p.value} condition {n}", kind="fact", evidence_ids=[ev.evidence_id], limitations=["One original trial"])
            claims.append(claim)
            support = c.ClaimSupport(claim_id=claim.claim_id, claim_hash=c.canonical_hash(claim), evidence_hashes={ev.evidence_id: c.canonical_hash(ev)}, support_status="supported", reason="Quote confirms trial conditions", verifier_version="1.0")
            supports[claim.claim_id] = support
        receipt = c.RetrievalReceipt(query_id=f"{p.value}-q", query_text=f"{p.value} original trial", queries_hash=c.canonical_hash({"query": p.value}), provider="pdf", corpus_version="1.0", index_version="1.0", filters={"company_id": "acme", "domain": "Physical AI / Robotics"}, ranked_hits=[c.RankedHit(source_id=e.source_id, chunk_id=e.chunk_id, rank=n + 1, score=0.8) for n, e in enumerate(items)], outcome="success", ts=NOW)
        result = c.PerspectiveResult(task_id=f"seed-{p.value}", perspective=p, attempt=1, claims=claims, evidence_ids=[e.evidence_id for e in items], answered_questions={q: [claims[n].claim_id] for n, q in enumerate(c.REQUIRED_QUESTIONS[p])}, gaps=[], limitations=["Only original trial conditions; no contradictory evidence found in these sources"], retrieval_receipts=[receipt] if p == c.Perspective.TECHNOLOGY_MATURITY else [])
        results[p] = result
        support_refs = [await store.save(supports[cl.claim_id], key=f"supports/{cl.claim_id}.json") for cl in claims]
        verdict = c.SufficiencyVerdict(perspective=p, structural_pass=True, semantic_pass=True, gaps=[], evidence_ids=result.evidence_ids, claim_support_refs=support_refs, reason="Both questions supported by original evidence", rubric_version="1.0")
        verdicts[p] = verdict
        result_ref = await store.save(result, key=f"results/{p.value}.json")
        verdict_ref = await store.save(verdict, key=f"verdicts/{p.value}.json")
        state["perspectives"][p] = c.PerspectiveSlot(status="sufficient", latest_result_ref=result_ref, result_hash=result.result_hash, sufficiency_ref=verdict_ref, gap_ids=[], source_count=2, publisher_group_count=2, answered_question_ids=list(c.REQUIRED_QUESTIONS[p]), attempt=1)
        state["research_attempts"][p] = 1
    target_claim = c.Claim(claim_id="target-conditions", perspective="market", text="Target is unlisted, Seed to Series C and has not exited", kind="fact", evidence_ids=[evidence[0].evidence_id], limitations=["As of the cited confirmation"])
    eligibility = c.EligibilityResult(task_id="eligible-seed", unlisted=True, funding_stage=True, exit_status=True, verdict="eligible", claims=[target_claim], gaps=[])
    state["eligibility_status"] = "eligible"
    state["eligibility_ref"] = await store.save(eligibility, key="eligibility.json")
    return results, verdicts, c.EvidenceBundle(sources=sources, evidence=evidence), supports, eligibility


async def assessor(verdicts):
    async def assess(request):
        return list(verdicts.values())
    return assess


def draft(state):
    c = contracts()
    return c.DraftReport(revision=state["report_revision"], evidence_epoch=state["evidence_epoch"], sections=[dict(section_id=s.value, title=s.value, paragraphs=[]) for s in c.SECTION_ORDER], claim_ids=[], citations=[], document_ref=None)


def evaluation(report, *, passing=True, findings=None):
    c = contracts()
    findings = findings or []
    return c.EvalVerdict(report_revision=report.revision, report_hash=report.content_hash, evidence_epoch=report.evidence_epoch, structural_checks={"citations": True}, criterion_scores={k: 3 if passing else 2 for k in c.Criterion}, findings=findings, overall_pass=passing, repair_kind="none" if passing else "research" if any(f.repair_kind == "research" for f in findings) else "rewrite", rubric_version="1.0", judge_model="judge", prompt_hash="a" * 64)
