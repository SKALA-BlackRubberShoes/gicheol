"""Deterministic research tests; these do not claim live model/search validation."""
import asyncio
import copy
from datetime import date, datetime, timezone

import pytest

from main.agents.perspectives.domain import DomainAgent
from main.agents.perspectives.market import MarketAgent, PerspectiveModelOutputError, _key
from main.agents.perspectives.stakeholders import StakeholdersAgent
from main.agents.supervisor import contracts as c
from main.rag.neutral.search import SourceInaccessibleError
from main.rag.neutral.tools import StoredEvidenceResolver


NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)
DOMAIN = c.Domain.PHYSICAL_AI_ROBOTICS


def run(awaitable):
    return asyncio.run(awaitable)


class Store:
    def __init__(self):
        self.blobs = {}
        self.saved = []

    def body(self, key, text, media_type="text/plain"):
        data = text.encode()
        self.blobs[key] = data
        return c.ArtifactRef(relative_path=key, sha256=c.sha256_text(text), media_type=media_type)

    async def verify(self, ref):
        if self.blobs.get(ref.relative_path) is None or c.sha256_text(self.blobs[ref.relative_path].decode()) != ref.sha256:
            raise ValueError("actual body is missing or corrupt")

    async def save(self, payload, *, key, media_type="application/json"):
        self.saved.append((key, copy.deepcopy(payload)))
        return self.body(key, c.canonical_json(payload), media_type)

    async def load(self, ref):
        import json
        await self.verify(ref)
        return json.loads(self.blobs[ref.relative_path])


def source(store, sid, text="Acme deploys robots for customers.", *, group=None,
           published="2026-01-01", controlled=False, pdf=False):
    ref = store.body(f"bodies/{sid}.pdf" if pdf else f"bodies/{sid}.txt", text,
                     "application/pdf" if pdf else "text/plain")
    return c.Source(source_id=sid, canonical_url=f"https://{sid}.example/report.pdf" if pdf else f"https://{sid}.example/report",
                    original_file_id=f"{sid}.pdf" if pdf else None,
                    title=f"Original {sid}", publisher=sid, publisher_group=group or sid,
                    source_type="paper" if pdf else "institution", published_at=published,
                    retrieved_at=NOW, corporate_controlled=controlled,
                    sha256=ref.sha256, content_ref=ref)


def evidence(src, *, eid=None, quote="Acme deploys robots for customers.",
             company="acme", event="2026-01-01"):
    return c.Evidence(evidence_id=eid or f"e-{src.source_id}", source_id=src.source_id,
                      company_id=company, chunk_id=f"chunk-{src.source_id}",
                      page_number=2 if src.original_file_id else None,
                      quote=quote, event_date=event)


def request(p=c.Perspective.MARKET, **overrides):
    return c.ResearchRequest(task_id="run-market-a1", company_id="acme", perspective=p,
                            as_of_date=date(2026, 1, 2), attempt=1, corpus_version="corpus-1", **overrides)


class Tools:
    def __init__(self, *, records=(), web=(), outcome="empty", fetch_failure=False):
        self.records = list(records)
        self.web = list(web)
        self.outcome = outcome
        self.fetch_failure = fetch_failure
        self.calls = []
        self.fetches = []
        self.wrong_receipt = None

    async def retrieve(self, query, **kwargs):
        self.calls.append(("retrieve", query, kwargs))
        receipts = c.RetrievalReceipt(query_id="pdf-" + c.canonical_hash(query)[:24],
            query_text=query, queries_hash=c.canonical_hash([query]), provider="actual-pdf-adapter",
            corpus_version="corpus-1", index_version="index-1",
            filters={"company_id": kwargs["company_id"], "domain": kwargs["domain"].value},
            ranked_hits=[c.RankedHit(source_id=s.source_id, chunk_id=e.chunk_id,
                                    rank=n + 1, score=0.7) for n, (s, e) in enumerate(self.records)],
            outcome="success" if self.records else self.outcome, ts=NOW)
        if self.wrong_receipt:
            raw = receipts.model_dump(mode="json")
            raw.update(self.wrong_receipt)
            receipts = c.RetrievalReceipt.model_validate(raw)
        return c.RetrievalResult(sources=[s for s, _ in self.records],
                                evidence=[e for _, e in self.records], receipt=receipts)

    async def search(self, query, **kwargs):
        self.calls.append(("search", query, kwargs))
        return c.SearchResult(outcome="success" if self.web else self.outcome,
            hits=[c.SearchHit(source_id=s.source_id, url=s.canonical_url,
                             title=s.title, snippet="Fabricated snippet: company sold a million robots.") for s, _ in self.web])

    async def fetch(self, hit):
        self.fetches.append(hit.source_id)
        if self.fetch_failure:
            raise PermissionError("paywall")
        src, text = next(pair for pair in self.web if pair[0].source_id == hit.source_id)
        return c.FetchedContent(source=src, text=text)


class LLM:
    def __init__(self, mode="normal"):
        self.mode = mode
        self.payloads = []

    async def generate(self, *, model, system_prompt, payload, response_model):
        assert model == "fixture-model"
        assert response_model is c.PerspectiveResult
        assert "unverified" in system_prompt
        self.payloads.append(copy.deepcopy(payload))
        req = c.ResearchRequest.model_validate(payload["request"])
        eids = [e["evidence_id"] for e in payload["evidence"]]
        questions = payload["required_questions"]
        if self.mode == "fabricated":
            eids = ["invented-citation"]
        kind = "unknown" if self.mode == "unknown" else "inference" if self.mode == "inference-without-limit" else "fact"
        claims = [c.Claim(claim_id=f"claim-{n}", perspective=req.perspective,
                   text="투자 추천" if self.mode == "recommendation" else f"Acme observed response to {q}.",
                   kind=kind, evidence_ids=[] if kind == "unknown" else [eids[min(n, len(eids)-1)]],
                   limitations=["The searched sources did not establish this question."] if kind == "unknown" else [])
                   for n, q in enumerate(questions)]
        raw = dict(task_id=req.task_id, perspective=req.perspective.value, attempt=req.attempt,
            claims=[cl.model_dump(mode="json") for cl in claims], evidence_ids=eids,
            answered_questions={q: [cl.claim_id] for q, cl in zip(questions, claims)},
            gaps=[], limitations=["Field outcomes beyond the original observation remain unverified."],
            retrieval_receipts=[])
        if self.mode == "wrong-task":
            raw["task_id"] = "a-stale-task"
        elif self.mode == "wrong-attempt":
            raw["attempt"] = req.attempt + 1
        elif self.mode == "wrong-perspective":
            raw["perspective"] = "technology_maturity"
        elif self.mode == "wrong-hash":
            raw["result_hash"] = "0" * 64
        elif self.mode == "malformed":
            raw["score"] = 100
        elif self.mode in {"cross-gap", "conflict"}:
            if self.mode == "cross-gap":
                raw["evidence_ids"] = eids[:2]
            raw["gaps"] = [c.Gap(gap_id="model-gap", perspective=c.Perspective.DOMAIN_APPLICATION if self.mode == "cross-gap" else req.perspective,
                kind=c.GapKind.MISSING_EVIDENCE if self.mode == "cross-gap" else c.GapKind.CONFLICTING_EVIDENCE,
                question="Verify the operating condition using both original observations.",
                severity="critical", evidence_ids=eids[-1:] if self.mode == "cross-gap" else eids[:2]).model_dump(mode="json")]
        return raw


def setup(agent=MarketAgent, *, mode="normal", records=2, web=False, **kw):
    store = Store()
    pairs = []
    for n in range(records):
        s = source(store, f"original-{n}", pdf=not web)
        pairs.append((s, "Acme deploys robots for customers.") if web else (s, evidence(s)))
    tools = Tools(web=pairs if web else (), records=() if web else pairs)
    llm = LLM(mode)
    worker = agent(tools=tools, llm=llm, domain=DOMAIN, model="fixture-model", store=store, **kw)
    return worker, tools, llm, store


def acquired_source(store, sid, text, **overrides):
    src = source(store, sid, text)
    payload = {"text": text, "pages": None, "body_verified": True,
               "company_ids": ["acme"], "domain": DOMAIN.value,
               "url": str(src.canonical_url), "acquisition": "http_body", **overrides}
    ref = run(store.save(payload, key=f"content/{sid}.json"))
    return src.model_copy(update={"content_ref": ref, "sha256": ref.sha256})


@pytest.mark.parametrize("agent,p", [(MarketAgent, c.Perspective.MARKET),
    (StakeholdersAgent, c.Perspective.STAKEHOLDERS), (DomainAgent, c.Perspective.DOMAIN_APPLICATION)])
def test_three_workers_bind_questions_receipts_and_leave_support_to_assessor(agent, p):
    worker, tools, llm, store = setup(agent)
    result = run(worker(request(p)))
    assert set(result.answered_questions) == set(c.REQUIRED_QUESTIONS[p])
    assert len(result.claims) == 2 and not result.gaps
    assert result.result_hash == c.semantic_hash(result)
    assert len(result.retrieval_receipts) == 6
    assert any(r.provider == "actual-pdf-adapter" and r.outcome == "success" for r in result.retrieval_receipts)
    assert all("support_status" not in claim.model_dump() for claim in result.claims)
    assert len(llm.payloads) == 1
    assert any(key == "evidence/e-original-0.json" for key, _ in store.saved)
    distinct = {q for _, q, _ in tools.calls}
    assert len(distinct) == 3
    assert all(k["company_id"] == "acme" and k["domain"] is DOMAIN and k["as_of_date"] == date(2026, 1, 2) and k["top_k"] == 5 for _, _, k in tools.calls)


def test_real_web_bodies_are_exact_quotes_persisted_and_snippets_are_never_cited():
    worker, tools, llm, store = setup(web=True, company_name="Acme", official_url="https://acme.example", company_id="acme")
    result = run(worker(request()))
    assert len(tools.fetches) == 2
    assert len(result.evidence_ids) == 2 and not result.gaps
    saved = {key: value for key, value in store.saved}
    for eid in result.evidence_ids:
        item = c.Evidence.model_validate(saved[f"evidence/{eid}.json"])
        assert item.quote == "Acme deploys robots for customers."
        assert item.quote_hash == c.sha256_text(item.quote)
        assert "million" not in item.quote
    assert all('"Acme"' in query and "https://acme.example" in query for _, query, _ in tools.calls)


@pytest.mark.parametrize("agent,p", [(MarketAgent, c.Perspective.MARKET),
    (StakeholdersAgent, c.Perspective.STAKEHOLDERS), (DomainAgent, c.Perspective.DOMAIN_APPLICATION)])
def test_actual_web_character_offsets_include_unicode_and_stripping(agent, p):
    worker, tools, llm, store = setup(agent, web=True, company_name="Acme")
    body = "\u2002 \nAcme는 로봇 pilot을 운영했습니다. Acme deployed robots.\n\t "
    tools.web = [(source(store, f"span-{n}", body), body) for n in range(2)]
    result = run(worker(request(p)))
    assert len(result.evidence_ids) == 2
    for raw in llm.payloads[0]["evidence"]:
        item = c.Evidence.model_validate(raw)
        prefix, span = item.chunk_id.rsplit(":", 1)
        start, end = map(int, span.split("-"))
        assert prefix == item.source_id
        assert start == 3 and body[start:end] == body.strip() == item.quote
        assert end == len(body.rstrip())
        assert worker.evidence_refs[item.evidence_id].relative_path == f"evidence/{item.evidence_id}.json"
        assert worker.source_refs[item.source_id].relative_path == f"sources/{item.source_id}.json"


def test_full_verified_json_body_beyond_fetch_prefix_is_registered_before_model():
    worker, tools, llm, store = setup(web=True, company_name="Acme")
    body = "다른 현장의 배경 자료. " * 1800 + "\u2002 \nAcme deployed robots at a customer site.\n\t"
    assert body.index("Acme") > 20000
    tools.web = [(acquired_source(store, f"full-{n}", body), body[:20000]) for n in range(2)]
    resolver = StoredEvidenceResolver(store=store, company_id="acme", domain=DOMAIN)
    worker = MarketAgent(tools=tools, llm=llm, domain=DOMAIN, model="fixture-model", store=store,
                         resolver=resolver, company_name="Acme")
    real_generate = llm.generate

    async def check_registered(**kwargs):
        ids = [e["evidence_id"] for e in kwargs["payload"]["evidence"]]
        assert ids and set(ids) <= resolver.evidence_refs.keys()
        bundle = await resolver.resolve(ids)
        assert all("Acme" in e.quote for e in bundle.evidence)
        return await real_generate(**kwargs)

    llm.generate = check_registered
    result = run(worker(request()))
    bundle = run(resolver.resolve(result.evidence_ids))
    assert worker.source_refs is resolver.source_refs and worker.evidence_refs is resolver.evidence_refs
    for item in bundle.evidence:
        start, end = map(int, item.chunk_id.rsplit(":", 1)[1].split("-"))
        assert start > 20000 and body[start:end] == item.quote
    restored = StoredEvidenceResolver(store=store, company_id="acme", domain=DOMAIN,
                                      source_refs=dict(worker.source_refs), evidence_refs=dict(worker.evidence_refs))
    assert run(restored.resolve(result.evidence_ids)) == bundle


def test_actual_save_refs_are_retained_without_assuming_requested_paths():
    worker, tools, _, store = setup(web=True, company_name="Acme")
    real_save = store.save

    async def redirected_save(payload, *, key, media_type="application/json"):
        return await real_save(payload, key=f"actual-registry/{key}", media_type=media_type)

    store.save = redirected_save
    result = run(worker(request()))
    assert set(result.evidence_ids) == worker.evidence_refs.keys()
    assert all(ref.relative_path.startswith("actual-registry/") for ref in [*worker.source_refs.values(), *worker.evidence_refs.values()])
    for eid, ref in worker.evidence_refs.items():
        assert run(store.load(ref))["evidence_id"] == eid


@pytest.mark.parametrize("bad", [{"company_ids": ["other"]}, {"company_ids": "acme-other"},
    {"domain": "Energy"}, {"body_verified": False}, {"url": "https://different.example/article"}])
def test_acquired_json_scope_or_url_tampering_never_exposes_evidence(bad):
    worker, tools, llm, store = setup(web=True, company_name="Acme")
    body = "Acme deployed robots at a customer site."
    tools.web = [(acquired_source(store, "corrupt-body", body, **bad), body)]
    with pytest.raises(ValueError, match="acquired body"):
        run(worker(request()))
    assert not llm.payloads and not worker.evidence_refs


def test_fabricated_fetch_prefix_cannot_override_verified_acquired_body():
    worker, tools, llm, store = setup(web=True)
    tools.web = [(acquired_source(store, "body-prefix", "Acme deployed robots."), "Acme sold a million robots.")]
    with pytest.raises(ValueError, match="prefix"):
        run(worker(request()))
    assert not llm.payloads and not worker.evidence_refs


def test_acquired_http_redirect_is_allowed_only_with_matching_body_provenance():
    worker, tools, _, store = setup(web=True, company_name="Acme")
    body = "Acme deployed robots."
    tools.web = [(acquired_source(store, f"redirect-{n}", body,
                                 url=f"https://resolved-{n}.example/article"), body) for n in range(2)]
    real_fetch = tools.fetch

    async def redirected_fetch(hit):
        fetched = await real_fetch(hit)
        index = hit.source_id.rsplit("-", 1)[1]
        return c.FetchedContent(source=fetched.source.model_copy(update={
            "canonical_url": c.HttpUrl(f"https://resolved-{index}.example/article")}), text=fetched.text)

    tools.fetch = redirected_fetch
    result = run(worker(request()))
    assert len(result.evidence_ids) == 2


def test_resolver_refuses_rehashed_fabricated_quote_and_wrong_actual_span():
    worker, tools, llm, store = setup(web=True, company_name="Acme")
    body = "\n Acme deployed robots at a customer site. \t"
    tools.web = [(acquired_source(store, f"verified-{n}", body), body) for n in range(2)]
    resolver = StoredEvidenceResolver(store=store, company_id="acme", domain=DOMAIN)
    worker = MarketAgent(tools=tools, llm=llm, domain=DOMAIN, model="fixture-model", store=store,
                         resolver=resolver, company_name="Acme")
    result = run(worker(request()))
    eid = result.evidence_ids[0]
    original = c.Evidence.model_validate(run(store.load(worker.evidence_refs[eid])))
    for update in [{"quote": "Acme fabricated the observation.", "quote_hash": None},
                   {"chunk_id": f"{original.source_id}:0-{len(original.quote)}"}]:
        altered = c.Evidence.model_validate({**original.model_dump(mode="json"), **update})
        resolver.evidence_refs[eid] = run(store.save(altered.model_dump(mode="json"), key=f"tampered/{eid}.json"))
        with pytest.raises(ValueError, match="WEB_LOCATION_MISMATCH"):
            run(resolver.resolve([eid]))


def test_actual_source_inaccessible_error_preserves_access_outcome_without_model():
    worker, tools, llm, _ = setup(web=True)

    async def inaccessible(hit):
        raise SourceInaccessibleError("SOURCE_INACCESSIBLE")

    tools.fetch = inaccessible
    result = run(worker(request()))
    assert not result.evidence_ids and not llm.payloads
    assert any(r.provider == "web-body" and r.outcome == "source_inaccessible" for r in result.retrieval_receipts)


@pytest.mark.parametrize("mode", ["fabricated", "wrong-task", "wrong-attempt", "wrong-perspective", "wrong-hash", "malformed", "recommendation", "inference-without-limit"])
def test_malformed_or_fabricated_model_output_fails_closed_after_one_correction(mode):
    worker, _, llm, _ = setup(mode=mode)
    with pytest.raises(PerspectiveModelOutputError):
        run(worker(request()))
    assert len(llm.payloads) == 2
    assert "correction" in llm.payloads[1]


def test_future_publication_and_event_are_not_cited_or_kept_in_receipt_hits():
    worker, tools, llm, store = setup()
    first, second = tools.records
    future_src = first[0].model_copy(update={"published_at": date(2026, 1, 3)})
    future_evidence = second[1].model_copy(update={"event_date": date(2026, 1, 3)})
    tools.records = [(future_src, first[1]), (second[0], future_evidence)]
    result = run(worker(request()))
    assert not result.claims and not result.evidence_ids and not llm.payloads
    assert all(not r.ranked_hits and r.outcome == "empty" for r in result.retrieval_receipts)
    assert any("after 2026-01-02" in limitation for limitation in result.limitations)
    assert not store.saved


def test_excluded_sources_are_removed_from_search_fetch_evidence_and_receipts():
    worker, tools, llm, _ = setup(web=True)
    result = run(worker(request(excluded_source_ids=["original-0"])))
    assert tools.fetches == ["original-1"]
    assert all(hit.source_id != "original-0" for receipt in result.retrieval_receipts for hit in receipt.ranked_hits)
    assert all(e["source_id"] != "original-0" for e in llm.payloads[0]["evidence"])
    assert any(g.kind == c.GapKind.SOURCE_BIAS for g in result.gaps)


@pytest.mark.parametrize("outcome", ["empty", "source_inaccessible", "error"])
def test_no_evidence_distinguishes_empty_inaccessible_and_error_without_model(outcome):
    worker, tools, llm, _ = setup(records=0)
    tools.outcome = outcome
    result = run(worker(request()))
    assert not result.claims and not llm.payloads
    assert {r.outcome for r in result.retrieval_receipts} == {outcome}
    assert {g.kind for g in result.gaps} >= {c.GapKind.MISSING_QUESTION, c.GapKind.MISSING_EVIDENCE, c.GapKind.SOURCE_BIAS}
    assert any(outcome in g.question for g in result.gaps if g.kind == c.GapKind.MISSING_EVIDENCE)


def test_paywalled_fetch_does_not_turn_snippet_into_evidence():
    worker, tools, llm, store = setup(web=True)
    tools.fetch_failure = True
    result = run(worker(request()))
    assert not result.claims and not llm.payloads and not store.saved
    assert any(r.provider == "web-body" and r.outcome == "source_inaccessible" for r in result.retrieval_receipts)
    assert any("snippet was not used" in limitation for limitation in result.limitations)


def test_new_web_evidence_without_store_is_explicitly_unavailable():
    worker, tools, llm, _ = setup(web=True)
    worker.store = None
    result = run(worker(request()))
    assert not result.evidence_ids and not llm.payloads
    assert any("requires an injected ArtifactStore" in limitation for limitation in result.limitations)


def test_wrong_company_body_despite_search_filter_is_not_attributed():
    worker, tools, llm, store = setup(web=True, company_name="Acme")
    tools.web = [(source(store, f"other-{n}", "Other Corp deploys robots."), "Other Corp deploys robots.") for n in range(2)]
    result = run(worker(request()))
    assert not result.evidence_ids and not llm.payloads
    assert any("does not identify the bound company Acme" in limitation for limitation in result.limitations)


def test_company_mention_after_first_chunk_produces_exact_target_containing_quote():
    worker, tools, llm, store = setup(web=True, company_name="Acme")
    body = "Rival Corp deploys robots. " + "Background filler. " * 180 + "Acme piloted a robot at a customer site."
    tools.web = [(source(store, f"long-{n}", body), body) for n in range(2)]
    result = run(worker(request()))
    assert len(result.evidence_ids) == 2
    for item in llm.payloads[0]["evidence"]:
        assert "Acme" in item["quote"] and item["quote"] in body


@pytest.mark.parametrize("suffix", ["는", "의", "이", "에", "를", "와"])
def test_korean_company_particles_match_and_same_prefix_other_company_does_not(suffix):
    worker, tools, llm, store = setup(web=True, company_name="아크미")
    body = f"아크미{suffix} 고객의 생산 현장에서 로봇을 시험했습니다."
    wrong_body = "아크미다른기업은 다른 로봇을 배포했습니다."
    tools.web = [(source(store, "korean-target", body), body),
                 (source(store, "korean-other", wrong_body), wrong_body)]
    result = run(worker(request()))
    assert len(result.evidence_ids) == 1
    assert llm.payloads[0]["evidence"][0]["quote"] == body
    assert any("does not identify" in limitation for limitation in result.limitations)


def test_fetched_url_mismatch_cannot_become_citation():
    worker, tools, _, store = setup(web=True)
    original_fetch = tools.fetch

    async def redirected_fetch(hit):
        result = await original_fetch(hit)
        return c.FetchedContent(source=result.source.model_copy(update={"canonical_url": c.HttpUrl("https://different.example/article")}), text=result.text)

    tools.fetch = redirected_fetch
    with pytest.raises(ValueError, match="identity differs"):
        run(worker(request()))


def test_real_body_hash_mismatch_fails_instead_of_using_any_quote():
    worker, tools, _, _ = setup(web=True)
    tools.web[0] = (tools.web[0][0], "Acme quote altered after fetch.")
    with pytest.raises(ValueError, match="body hash"):
        run(worker(request()))


def test_actual_artifact_missing_or_corrupt_fails_closed():
    worker, tools, _, store = setup()
    store.blobs.pop(tools.records[0][0].content_ref.relative_path)
    with pytest.raises(ValueError, match="missing or corrupt"):
        run(worker(request()))


class ProviderRejection(Exception):
    def __init__(self, status):
        self.status_code = status


@pytest.mark.parametrize("boundary", ["retrieve", "search", "model", "fetch"])
def test_provider_401_is_classified_and_store_permission_remains_an_artifact_error(boundary):
    from main.graph.supervisor_nodes import WorkerFailure
    worker, tools, llm, store = setup(web=boundary == "fetch")

    async def rejected(*args, **kwargs):
        raise ProviderRejection(401)

    if boundary == "model":
        llm.generate = rejected
    else:
        setattr(tools, boundary, rejected)
    with pytest.raises(WorkerFailure) as caught:
        run(worker(request()))
    assert caught.value.error.code == c.TerminalReason.CONFIG_OR_AUTH_ERROR
    assert not caught.value.error.retriable


def test_injected_classified_worker_failure_is_preserved_and_not_retried():
    from main.graph.supervisor_nodes import WorkerFailure
    worker, tools, _, _ = setup()
    expected = WorkerFailure(c.ErrorSummary(code="CORPUS_DOMAIN_UNAVAILABLE", node="market", retriable=False,
                                           message="This domain is absent from the corpus."))

    async def unavailable(*args, **kwargs):
        raise expected

    tools.retrieve = unavailable
    with pytest.raises(WorkerFailure) as caught:
        run(worker(request()))
    assert caught.value is expected


def test_filesystem_permission_is_not_provider_auth_and_fetch_403_is_inaccessible():
    worker, tools, _, store = setup()

    async def forbidden_file(ref):
        raise PermissionError("read access")

    store.verify = forbidden_file
    with pytest.raises(PermissionError):
        run(worker(request()))
    worker, tools, llm, _ = setup(web=True)

    async def paywall(hit):
        raise ProviderRejection(403)

    tools.fetch = paywall
    result = run(worker(request()))
    assert not result.evidence_ids and not llm.payloads
    assert any(r.outcome == "source_inaccessible" for r in result.retrieval_receipts)


@pytest.mark.parametrize("bad", [{"corpus_version": "wrong-corpus"},
    {"filters": {"company_id": "other", "domain": DOMAIN.value}}, {"query_text": "a different query"},
    {"filters": {"company_id": "acme", "domain": DOMAIN.value, "as_of_date": "2026-01-03"}}])
def test_wrong_pdf_receipt_binding_is_rejected(bad):
    worker, tools, _, _ = setup()
    tools.wrong_receipt = bad
    with pytest.raises(ValueError, match="receipt"):
        run(worker(request()))


def test_pdf_quote_requires_company_actual_page_and_receipt_hit():
    worker, tools, _, _ = setup()
    s, e = tools.records[0]
    tools.records[0] = (s, e.model_copy(update={"company_id": "other"}))
    with pytest.raises(ValueError, match="company"):
        run(worker(request()))
    tools.records[0] = (s, e.model_copy(update={"page_number": None}))
    with pytest.raises(ValueError, match="page"):
        run(worker(request()))


def test_undated_evidence_cannot_be_self_presented_as_date_valid_fact():
    worker, tools, llm, _ = setup()
    tools.records = [(s.model_copy(update={"published_at": None}), e.model_copy(update={"event_date": None})) for s, e in tools.records]
    with pytest.raises(PerspectiveModelOutputError):
        run(worker(request()))
    assert len(llm.payloads) == 2


def test_actual_pdf_hit_is_required_even_when_evidence_dto_looks_valid():
    worker, tools, _, _ = setup()
    real_retrieve = tools.retrieve

    async def mismatched_retrieve(query, **kwargs):
        result = await real_retrieve(query, **kwargs)
        data = result.model_dump(mode="json")
        data["receipt"]["ranked_hits"] = data["receipt"]["ranked_hits"][1:]
        return c.RetrievalResult.model_validate(data)

    tools.retrieve = mismatched_retrieve
    with pytest.raises(ValueError, match="receipt hit"):
        run(worker(request()))


def test_republished_sources_are_not_counted_as_independent():
    worker, tools, _, _ = setup()
    tools.records = [(s.model_copy(update={"publisher_group": "one-publisher", "corporate_controlled": True}), e) for s, e in tools.records]
    result = run(worker(request()))
    assert any(g.kind == c.GapKind.SOURCE_BIAS for g in result.gaps)


def test_unknown_answers_do_not_satisfy_fixed_questions():
    worker, _, _, _ = setup(mode="unknown")
    result = run(worker(request()))
    assert not result.answered_questions
    assert len([g for g in result.gaps if g.kind == c.GapKind.MISSING_QUESTION]) == 2


def test_conflicts_and_cross_view_gaps_preserve_vetted_refs():
    worker, _, _, _ = setup(mode="conflict")
    result = run(worker(request()))
    assert result.gaps[0].kind == c.GapKind.CONFLICTING_EVIDENCE
    assert len(result.gaps[0].evidence_ids) == 2
    assert not any("No conflicting" in text for text in result.limitations)
    worker, _, _, _ = setup(mode="cross-gap", records=3)
    result = run(worker(request()))
    gap = next(g for g in result.gaps if g.gap_id == "model-gap")
    assert gap.perspective == c.Perspective.DOMAIN_APPLICATION
    assert set(gap.evidence_ids) <= set(result.evidence_ids)
    assert len(result.evidence_ids) == 3


def test_followup_loads_verified_prior_and_honors_attempt_gap_hints_and_instructions():
    worker, tools, llm, store = setup()
    first = run(worker(request()))
    ref = run(store.save(first.model_dump(mode="json"), key="results/prior.json"))
    tools.calls.clear()
    followup = c.ResearchRequest(task_id="run-market-a2", company_id="acme", perspective=c.Perspective.MARKET,
        as_of_date="2026-01-02", attempt=2, corpus_version="corpus-1", prior_result_ref=ref,
        gap_ids=["gap-customer-demand"], query_hints=["independent customer usage", "regulator cost study", "partner integration", "ignored fourth hint"],
        instructions=["Resolve adoption burden before inferring demand."], excluded_source_ids=["original-0"])
    result = run(worker(followup))
    assert result.attempt == 2 and result.task_id == "run-market-a2"
    queries = {q for _, q, _ in tools.calls}
    assert len(queries) == 3 and all("follow-up 2" in q and "gap-customer-demand" in q for q in queries)
    assert not any("ignored fourth" in q for q in queries)
    assert llm.payloads[-1]["request"]["instructions"] == followup.instructions
    assert llm.payloads[-1]["prior_result"]["result_hash"] == first.result_hash
    assert "e-original-0" not in result.evidence_ids


def test_constructor_and_request_boundary_reject_misbound_identity():
    worker, _, _, _ = setup(company_id="different-company")
    with pytest.raises(ValueError, match="company differs"):
        run(worker(request()))
    with pytest.raises(ValueError, match="perspective"):
        run(worker(request(c.Perspective.STAKEHOLDERS)))
    assert len(_key("evidence", "a" * 256).split("/")[-1]) < 100
    assert ".." not in _key("sources", "../../escape")
