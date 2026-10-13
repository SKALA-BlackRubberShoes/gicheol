"""Eligibility checks use fetched original bodies, not model assertions."""
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from main.agents.eligibility.agent import EligibilityAgent, EligibilityModelError
from main.agents.supervisor import contracts as c


FIXTURES = json.loads((Path(__file__).resolve().parents[3] / "fixtures/v2/sooyoung/eligibility.json").read_text())
NOW = datetime(2026, 10, 13, tzinfo=timezone.utc)


class MemoryStore:
    def __init__(self):
        self.payloads, self.originals = {}, {}

    async def verify(self, ref):
        value = self.originals[ref.relative_path]
        if c.sha256_text(value) != ref.sha256:
            raise ValueError("original byte hash mismatch")

    async def save(self, payload, *, key, media_type="application/json"):
        self.payloads[key] = payload
        return c.ArtifactRef(relative_path=key, sha256=c.canonical_hash(payload), media_type=media_type)


def content(body, store, *, source_id="original-1", published="2026-10-13", media="text/plain", url="https://registry.example/acme"):
    ref = c.ArtifactRef(relative_path="originals/" + c.sha256_text(body) + ".txt", sha256=c.sha256_text(body), media_type=media)
    store.originals[ref.relative_path] = body
    source = c.Source(source_id=source_id, canonical_url=url, title="Dated company register", publisher="Registry",
                      publisher_group="registry", source_type="institution", published_at=published, retrieved_at=NOW,
                      corporate_controlled=False, sha256=ref.sha256, content_ref=ref)
    return c.FetchedContent(source=source, text=body)


class Tools:
    def __init__(self, items=(), *, search_error=None, fetch_error=None, snippet=None):
        self.items = list(items)
        self.search_error, self.fetch_error, self.snippet = search_error, fetch_error, snippet
        self.queries, self.fetched = [], []

    async def search(self, query, **kwargs):
        self.queries.append((query, kwargs))
        if self.search_error:
            raise self.search_error
        hits = [c.SearchHit(source_id=item.source.source_id, url=item.source.canonical_url, title=item.source.title,
                            snippet=self.snippet) for item in self.items]
        return c.SearchResult(hits=hits, outcome="success" if hits else "empty")

    async def fetch(self, hit):
        self.fetched.append(hit)
        if self.fetch_error:
            raise self.fetch_error
        return next(item for item in self.items if item.source.canonical_url == hit.url)

    async def retrieve(self, *args, **kwargs):
        raise AssertionError("Eligibility must not invent PDF evidence from a web snippet")


class Model:
    def __init__(self, response=None, *, delay=0):
        self.response, self.delay, self.calls = response, delay, []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        payload = kwargs["payload"]
        if self.response:
            return self.response(payload)
        return c.EligibilityResult(task_id=payload["task_id"], unlisted="unknown", funding_stage="unknown",
                                   exit_status="unknown", verdict="unknown", claims=[], gaps=[])


def request(**changes):
    return c.EligibilityRequest(task_id="eligibility-task", company_id="acme", company_name=changes.pop("company_name", "Acme"),
        official_url="https://acme.example", as_of_date="2026-10-13", attempt=changes.pop("attempt", 1), **changes)


def run_agent(items=(), *, store=None, tools=None, model=None, req=None, timeout=1):
    tools = tools or Tools(items)
    model = model or Model()
    worker = EligibilityAgent(tools=tools, llm=model, domain=c.Domain.PHYSICAL_AI_ROBOTICS,
                              model="fixture-model", store=store, timeout_seconds=timeout)
    return asyncio.run(worker(req or request())), tools, model


def test_all_three_conditions_need_original_dated_proof_and_persist_resolver_artifacts():
    store = MemoryStore()
    item = content(FIXTURES["eligible"], store, source_id="../../untrusted-source")
    result, tools, model = run_agent([item], store=store)
    assert result.verdict == "eligible"
    assert (result.unlisted, result.funding_stage, result.exit_status) == (True, True, True)
    assert len(tools.fetched) == 1 and len(model.calls) == 1
    assert model.calls[0]["response_model"] is c.EligibilityResult
    assert result.result_hash == c.EligibilityResult.model_validate(result.model_dump(mode="json")).result_hash
    for claim in result.claims:
        assert claim.perspective == c.Perspective.STAKEHOLDERS and "as_of_date=2026-10-13" in claim.text
        for evidence_id in claim.evidence_ids:
            evidence = c.Evidence.model_validate(store.payloads[f"evidence/{evidence_id}.json"])
            source = c.Source.model_validate(store.payloads[f"sources/{evidence.source_id}.json"])
            assert evidence.quote in item.text and evidence.company_id == "acme"
            assert evidence.quote_hash == c.sha256_text(evidence.quote)
            assert evidence.event_date == request().as_of_date and ".." not in source.source_id


@pytest.mark.parametrize("body, expected", [(FIXTURES["completed_exit"], False), (FIXTURES["mixed_negation_exit"], False)])
def test_verified_historic_exit_is_ineligible_even_with_unlisted_negation_elsewhere(body, expected):
    store = MemoryStore()
    result, _, _ = run_agent([content(body, store)], store=store)
    assert result.verdict == "ineligible" and result.exit_status is expected
    assert any("Acme was acquired" in claim.text for claim in result.claims)


@pytest.mark.parametrize("stage", ["Seed", "Series A", "Series B", "Series C"])
def test_allowed_latest_funding_stages(stage):
    store = MemoryStore()
    body = f"Acme latest funding stage is {stage} as of 2026-10-13."
    result, _, _ = run_agent([content(body, store)], store=store)
    assert result.funding_stage is True and result.verdict == "unknown"
    assert {gap.kind for gap in result.gaps} == {c.GapKind.ELIGIBILITY_UNKNOWN}


@pytest.mark.parametrize("stage", ["pre-seed", "Series D", "Series E"])
def test_verified_outside_stage_is_ineligible(stage):
    store = MemoryStore()
    result, _, _ = run_agent([content(f"Acme current funding stage is {stage} as of 2026-10-13.", store)], store=store)
    assert result.funding_stage is False and result.verdict == "ineligible"


@pytest.mark.parametrize("fixture", ["stale", "other_company", "negated_private"])
def test_stale_rival_and_negated_private_statements_do_not_establish_eligibility(fixture):
    store = MemoryStore()
    result, _, _ = run_agent([content(FIXTURES[fixture], store)], store=store)
    assert result.verdict == "unknown" and result.unlisted == "unknown"
    assert all(gap.perspective == "eligibility" and gap.severity == "critical" for gap in result.gaps)


@pytest.mark.parametrize("body", ["Acme raised a Series B round on 2025-01-01.",
    "Acme current funding stage is unknown; Acme plans a Series D round.",
    "Acme plans an IPO as of 2026-10-13.", "Acme is in M&A discussions as of 2026-10-13.",
    "Acme completed its acquisition of Beta on 2026-10-13.", "아크미는 인수를 완료했다.",
    "아크미는 인수 검토 중이다.", "Acme has no exit announcement as of 2026-10-13.",
    "Acme's rival Beta is unlisted as of 2026-10-13."])
def test_announcements_plans_acquirer_role_and_rival_are_not_current_eligibility_proof(body):
    store = MemoryStore()
    req = request(company_name="아크미") if body.startswith("아크미") else request()
    result, _, model = run_agent([content(body, store)], store=store, req=req)
    assert result.verdict == "unknown" and all(getattr(result, key) == "unknown" for key in ("unlisted", "funding_stage", "exit_status"))
    assert len(result.gaps) == 3 and not model.calls


def test_verified_contradiction_preserves_both_source_quotes_and_false_wins():
    store = MemoryStore()
    first = content("Acme is unlisted as of 2026-10-13.", store)
    second = content("Acme is publicly listed as of 2026-10-13.", store, source_id="other", url="https://exchange.example/acme")
    result, _, _ = run_agent([first, second], store=store)
    assert result.unlisted is False and result.verdict == "ineligible"
    conflict = next(gap for gap in result.gaps if gap.kind == c.GapKind.CONFLICTING_EVIDENCE)
    assert len(conflict.evidence_ids) == 2
    assert len([claim for claim in result.claims if "eligibility.unlisted=" in claim.text]) == 2


@pytest.mark.parametrize("fixture, expected", [("korean_eligible", "eligible"), ("korean_exit", "ineligible")])
def test_korean_company_particles_and_status_body(fixture, expected):
    store = MemoryStore()
    result, _, _ = run_agent([content(FIXTURES[fixture], store)], store=store, req=request(company_name="아크미"))
    assert result.verdict == expected


@pytest.mark.parametrize("published, body", [("2026-10-14", FIXTURES["eligible"]),
    (None, FIXTURES["eligible"]), ("2026-10-13", "Acme was acquired by BigCo on 2026-10-14.")])
def test_future_or_undated_proof_is_excluded(published, body):
    store = MemoryStore()
    result, _, model = run_agent([content(body, store, published=published)], store=store)
    assert result.verdict == "unknown" and not model.calls


def test_snippet_and_fetch_errors_never_become_evidence():
    store = MemoryStore()
    item = content("Body could not be retrieved", store)
    tools = Tools([item], fetch_error=OSError("unavailable"), snippet=FIXTURES["eligible"])
    result, _, model = run_agent(store=store, tools=tools)
    assert result.verdict == "unknown" and len(result.gaps) == 3 and not model.calls
    assert any("snippets" in limitation for claim in result.claims for limitation in claim.limitations)


def test_no_store_cannot_return_eligible_with_unresolvable_evidence():
    store = MemoryStore()
    result, _, model = run_agent([content(FIXTURES["eligible"], store)])
    assert result.verdict == "unknown" and not model.calls


def test_plain_body_hash_tampering_is_rejected():
    store = MemoryStore()
    item = content(FIXTURES["eligible"], store).model_copy(update={"text": "Acme is publicly listed as of 2026-10-13."})
    with pytest.raises(ValueError, match="body hash"):
        run_agent([item], store=store)


def test_model_memory_cannot_make_unproven_conditions_true():
    store = MemoryStore()
    model = Model(lambda payload: c.EligibilityResult(task_id=payload["task_id"], unlisted=True, funding_stage=True,
        exit_status=True, verdict="eligible", claims=[], gaps=[]))
    result, _, _ = run_agent([content("Acme latest funding stage is Series B as of 2026-10-13.", store)], store=store, model=model)
    assert result.funding_stage is True and result.unlisted == result.exit_status == "unknown"
    assert result.verdict == "unknown"


@pytest.mark.parametrize("failure", ["task", "hash", "evidence", "criterion"])
def test_invalid_model_task_hash_and_citation_bindings_rejected(failure):
    store = MemoryStore()
    def response(payload):
        data = dict(task_id=payload["task_id"], unlisted="unknown", funding_stage="unknown", exit_status="unknown",
                    verdict="unknown", claims=[], gaps=[])
        if failure == "task":
            data["task_id"] = "other-task"
        elif failure == "hash":
            data["result_hash"] = "0" * 64
        else:
            data["claims"] = [dict(claim_id="model-claim", perspective="stakeholders", text="eligibility.unlisted=true; as_of_date=2026-10-13",
                kind="fact", evidence_ids=["invented" if failure == "evidence" else payload["evidence"][0]["evidence_id"]], limitations=[])]
        return data
    model = Model(response)
    with pytest.raises((EligibilityModelError, ValueError)):
        run_agent([content("Acme latest funding stage is Series B as of 2026-10-13.", store)], store=store, model=model)


def test_retry_instructions_reach_actual_search_queries_and_model_payload():
    store = MemoryStore()
    req = request(attempt=2, gap_ids=["gap-stage"], instructions=["Use the securities registry latest funding stage record"])
    result, tools, model = run_agent([content(FIXTURES["eligible"], store)], store=store, req=req)
    assert result.verdict == "eligible"
    assert all("securities registry" in query for query, _ in tools.queries)
    assert all(params["company_id"] == "acme" and params["as_of_date"] == req.as_of_date for _, params in tools.queries)
    assert model.calls[0]["payload"]["gap_ids"] == req.gap_ids and model.calls[0]["payload"]["attempt"] == 2


def test_model_timeout_cannot_promote_eligibility_but_retains_verified_exit_violation():
    store = MemoryStore()
    positive, _, _ = run_agent([content(FIXTURES["eligible"], store)], store=store, model=Model(delay=1), timeout=.02)
    negative, _, _ = run_agent([content(FIXTURES["completed_exit"], store)], store=store, model=Model(delay=1), timeout=.02)
    assert positive.verdict == "unknown" and len(positive.gaps) == 3
    assert negative.verdict == "ineligible" and negative.exit_status is False


def test_authentication_error_remains_permanent_classified_failure():
    from main.graph.supervisor_nodes import WorkerFailure
    with pytest.raises(WorkerFailure) as captured:
        run_agent(tools=Tools(search_error=PermissionError("secret credential text")))
    assert captured.value.error.code == c.TerminalReason.CONFIG_OR_AUTH_ERROR
    assert not captured.value.error.retriable and "secret" not in str(captured.value)


def test_unpaged_pdf_fetch_is_not_eligible_proof():
    store = MemoryStore()
    result, _, model = run_agent([content(FIXTURES["eligible"], store, media="application/pdf")], store=store)
    assert result.verdict == "unknown" and not model.calls
    assert any("page-preserving" in limitation for claim in result.claims for limitation in claim.limitations)


def test_search_with_inconsistent_metadata_is_rejected():
    class Inconsistent(Tools):
        async def search(self, query, **kwargs):
            return c.SearchResult(hits=[], outcome="success")
    with pytest.raises(ValueError, match="outcome"):
        run_agent(tools=Inconsistent())


def test_conditional_and_prospective_statuses_cannot_establish_eligibility():
    store = MemoryStore()
    body = "As of 2026-10-13, Acme is unlisted if the reorganization is approved. Acme current funding stage is Series B subject to approval. Acme has not completed an exit."
    result, _, _ = run_agent([content(body, store)], store=store)
    assert result.unlisted == result.funding_stage == "unknown"
    assert result.exit_status is True and result.verdict == "unknown"


def test_source_and_evidence_identity_ignore_operational_retrieval_timestamps():
    store = MemoryStore()
    item = content(FIXTURES["eligible"], store)
    first, _, _ = run_agent([item], store=store)
    second_source = item.source.model_copy(update={"retrieved_at": datetime(2026, 10, 14, tzinfo=timezone.utc)})
    second, _, _ = run_agent([item.model_copy(update={"source": second_source})], store=store)
    assert {eid for claim in first.claims for eid in claim.evidence_ids} == {eid for claim in second.claims for eid in claim.evidence_ids}
    assert first.result_hash == second.result_hash


@pytest.mark.parametrize("body", ["If approved, Acme is unlisted as of 2026-10-13.",
    "Acme is unlisted?", "Acme has not completed an acquisition of Beta as of 2026-10-13.",
    "Acme current funding stage is Series B or Series D as of 2026-10-13.",
    "아크미의 현재 투자 단계는 시리즈 A가 아니라 시리즈 D다."])
def test_prefix_conditions_questions_specific_acquisitions_and_ambiguous_stage_are_unknown(body):
    store = MemoryStore()
    req = request(company_name="아크미") if body.startswith("아크미") else request()
    result, _, _ = run_agent([content(body, store)], store=store, req=req)
    assert result.verdict == "unknown" and all(getattr(result, key) == "unknown" for key in ("unlisted", "funding_stage", "exit_status"))
