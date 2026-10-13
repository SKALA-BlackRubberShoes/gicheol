"""The technology worker researches and preserves gaps; it never grants support."""
import asyncio
from datetime import timedelta

import pytest
from pydantic import ValidationError

from main.agents.perspectives.technology import TechnologyWorker
from main.agents.supervisor.contracts import Perspective, PerspectiveResult
from tests.fixtures.v2.suhyeon.store import (
    DOMAIN, FixtureLLM, FixtureTools, VerifiedJsonStore, request, retrieval_result, stored_source,
)


def worker(store, tools, llm, **kwargs):
    return TechnologyWorker(store=store, tools=tools, llm=llm, model="explicit-fixture-model",
                            company_id="acme", company_name="Acme", domain=DOMAIN,
                            corpus_version="fixture-v1", **kwargs)


def test_worker_returns_common_dtos_pdf_receipts_and_cross_view_gap(tmp_path):
    async def scenario():
        store = VerifiedJsonStore(tmp_path)
        source, evidence = await stored_source(store)
        tools = FixtureTools(retrieval_result(source=source, evidence=evidence))
        llm = FixtureLLM(cross_view_gap=True)
        result = await worker(store, tools, llm)(request(query_hints=["Acme validation", "Acme validation"]))
        assert isinstance(result, PerspectiveResult)
        assert len(tools.calls) == 3 and len(llm.calls) == 1
        assert set(result.answered_questions) == {"tech.principle", "tech.validation"}
        assert result.gaps[0].perspective == Perspective.MARKET
        assert result.gaps[0].query_hints == ["Acme customer demand"]
        assert result.evidence_ids == [evidence.evidence_id]
        assert len({r.queries_hash for r in result.retrieval_receipts}) == 1
        assert len({r.query_id for r in result.retrieval_receipts}) == len(tools.calls)
        assert [r.query_text for r in result.retrieval_receipts] == [query for query, _ in tools.calls]
        assert all(r.ranked_hits[0].chunk_id == evidence.chunk_id for r in result.retrieval_receipts)
        assert not hasattr(result, "support_status")
        assert "supported" not in result.model_dump_json()
        assert not hasattr(tools, "market") and not hasattr(tools, "writer")
        assert any(key.startswith("retrieval/tasks/task-tech/") for key in store.saved_keys)
        assert any("발행일" in text for text in result.limitations)
    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["empty", "source_inaccessible", "error"])
def test_no_evidence_preserves_actual_outcome_and_skips_llm(tmp_path, outcome):
    async def scenario():
        store = VerifiedJsonStore(tmp_path)
        tools, llm = FixtureTools(retrieval_result(outcome=outcome)), FixtureLLM()
        result = await worker(store, tools, llm)(request())
        assert result.claims == [] and result.evidence_ids == [] and result.answered_questions == {}
        assert not llm.calls
        assert {r.outcome for r in result.retrieval_receipts} == {outcome}
        assert len(result.gaps) == 3 and outcome in result.gaps[0].question
    asyncio.run(scenario())


@pytest.mark.parametrize("mode,error", [("invalid_citation", "TECHNOLOGY_CLAIM_SCOPE_OR_CITATION_MISMATCH"),
                                         ("wrong_perspective", "TECHNOLOGY_CLAIM_SCOPE_OR_CITATION_MISMATCH"),
                                         ("unknown_answer", "UNKNOWN_CLAIM_CANNOT_ANSWER_QUESTION"),
                                         ("wrong_question", "TECHNOLOGY_QUESTION_MISMATCH")])
def test_generated_invalid_citations_and_unknown_answers_are_rejected(tmp_path, mode, error):
    async def scenario():
        store = VerifiedJsonStore(tmp_path)
        source, evidence = await stored_source(store)
        tools = FixtureTools(retrieval_result(source=source, evidence=evidence))
        with pytest.raises(ValueError, match=error):
            await worker(store, tools, FixtureLLM(mode=mode))(request())
    asyncio.run(scenario())


def test_generated_self_support_verdict_is_rejected_by_private_response_schema(tmp_path):
    async def scenario():
        store = VerifiedJsonStore(tmp_path)
        source, evidence = await stored_source(store)
        with pytest.raises(ValidationError, match="support_status"):
            await worker(store, FixtureTools(retrieval_result(source=source, evidence=evidence)),
                         FixtureLLM(mode="self_support"))(request())
    asyncio.run(scenario())


@pytest.mark.parametrize("web_case,expected", [("empty", "empty"), ("future", "empty"),
                                               ("excluded", "empty"), ("inaccessible", "source_inaccessible")])
def test_web_policy_filtered_empty_and_http_body_inaccessible_are_distinct(tmp_path, web_case, expected):
    from main.agents.supervisor.contracts import FetchedContent, SearchHit, SearchResult
    from main.rag.neutral.search import SourceInaccessibleError
    async def scenario():
        store = VerifiedJsonStore(tmp_path)
        source, evidence = await stored_source(store, pdf=False)
        if web_case == "future":
            source = source.model_copy(update={"published_at": request().as_of_date + timedelta(days=1)})
        hit = SearchHit(source_id=source.source_id, url=source.canonical_url, title=source.title)
        class WebTools(FixtureTools):
            async def search(self, *args, **kwargs):
                return SearchResult(hits=[] if web_case == "empty" else [hit],
                                    outcome="empty" if web_case == "empty" else "success")
            async def fetch(self, hit):
                if web_case == "inaccessible":
                    raise SourceInaccessibleError("SOURCE_INACCESSIBLE")
                return FetchedContent(source=source, text=evidence.quote)
        tools, llm = WebTools(retrieval_result(outcome="empty")), FixtureLLM()
        exclusions = [source.source_id] if web_case == "excluded" else []
        result = await worker(store, tools, llm, include_web=True, max_queries=1)(
            request(query_hints=["Acme one query"], excluded_source_ids=exclusions))
        web_receipts = [r for r in result.retrieval_receipts if r.provider == "legacy_web_search_http_body"]
        assert len(web_receipts) == 1 and web_receipts[0].outcome == expected
        assert not llm.calls and result.evidence_ids == []
    asyncio.run(scenario())


@pytest.mark.parametrize("changes,error", [({"company_id": "other"}, "TECHNOLOGY_REQUEST_SCOPE_MISMATCH"),
                                           ({"perspective": "market"}, "TECHNOLOGY_REQUEST_SCOPE_MISMATCH"),
                                           ({"corpus_version": "different"}, "CORPUS_VERSION_MISMATCH")])
def test_worker_rejects_out_of_scope_requests_before_tools(tmp_path, changes, error):
    async def scenario():
        tools = FixtureTools(retrieval_result(outcome="empty"))
        with pytest.raises(ValueError, match=error):
            await worker(VerifiedJsonStore(tmp_path), tools, FixtureLLM())(request(**changes))
        assert not tools.calls
    asyncio.run(scenario())


def test_excluded_sources_do_not_reach_llm_or_resolver(tmp_path):
    async def scenario():
        store = VerifiedJsonStore(tmp_path)
        source, evidence = await stored_source(store)
        tools, llm = FixtureTools(retrieval_result(source=source, evidence=evidence)), FixtureLLM()
        result = await worker(store, tools, llm)(request(excluded_source_ids=[source.source_id]))
        assert result.evidence_ids == [] and result.claims == [] and not llm.calls
    asyncio.run(scenario())


def test_future_source_and_event_are_rejected(tmp_path):
    async def scenario():
        store = VerifiedJsonStore(tmp_path)
        source, evidence = await stored_source(store)
        for changed_source, changed_evidence, error in [
            (source.model_copy(update={"published_at": request().as_of_date + timedelta(days=1)}), evidence, "FUTURE_SOURCE_REJECTED"),
            (source, evidence.model_copy(update={"event_date": request().as_of_date + timedelta(days=1)}), "FUTURE_EVIDENCE_REJECTED"),
        ]:
            with pytest.raises(ValueError, match=error):
                await worker(store, FixtureTools(retrieval_result(source=changed_source, evidence=changed_evidence)), FixtureLLM())(request())
    asyncio.run(scenario())


def test_repeating_empty_queries_requires_new_hints_but_error_can_retry(tmp_path):
    async def scenario():
        store = VerifiedJsonStore(tmp_path)
        prior = await worker(store, FixtureTools(retrieval_result(outcome="empty")), FixtureLLM())(request())
        prior_ref = await store.save(prior.model_dump(mode="json"), key="prior/empty.json")
        with pytest.raises(ValueError, match="REPEATED_QUERIES_REQUIRE_NEW_HINTS"):
            await worker(store, FixtureTools(retrieval_result(outcome="empty")), FixtureLLM())(request(attempt=2, prior_result_ref=prior_ref))
        changed = await worker(store, FixtureTools(retrieval_result(outcome="empty")), FixtureLLM())(
            request(attempt=2, prior_result_ref=prior_ref, query_hints=["Acme new laboratory protocol"]))
        assert changed.attempt == 2
        failure = await worker(store, FixtureTools(retrieval_result(outcome="error")), FixtureLLM())(request())
        failure_ref = await store.save(failure.model_dump(mode="json"), key="prior/error.json")
        retry = await worker(store, FixtureTools(retrieval_result(outcome="error")), FixtureLLM())(
            request(attempt=2, prior_result_ref=failure_ref))
        assert retry.attempt == 2
    asyncio.run(scenario())


def test_result_hash_ignores_receipt_operational_timestamp(tmp_path):
    async def scenario():
        store = VerifiedJsonStore(tmp_path)
        source, evidence = await stored_source(store)
        result = await worker(store, FixtureTools(retrieval_result(source=source, evidence=evidence)), FixtureLLM())(request())
        body = result.model_dump(mode="json")
        body["retrieval_receipts"][0]["ts"] = "2026-02-01T00:00:00Z"
        assert PerspectiveResult.model_validate(body).result_hash == result.result_hash
        body["claims"][0]["text"] = "Changed claim must invalidate the stable result hash."
        with pytest.raises(ValidationError, match="result_hash"):
            PerspectiveResult.model_validate(body)
    asyncio.run(scenario())


@pytest.mark.parametrize("failure,code", [("IndexMismatchError", "INDEX_MISMATCH"),
                                          ("CorpusDomainUnavailableError", "CORPUS_DOMAIN_UNAVAILABLE"),
                                          ("ArtifactIntegrityError", "ARTIFACT_INTEGRITY_ERROR")])
def test_typed_retrieval_failures_return_permanent_result_envelope_and_preserve_receipt(tmp_path, failure, code):
    from main.rag.neutral import retrieval
    from main.agents.supervisor.contracts import ResultEnvelope
    async def scenario():
        store, llm = VerifiedJsonStore(tmp_path), FixtureLLM()
        receipt = retrieval_result(outcome="error").receipt
        error = getattr(retrieval, failure)("Explicit deterministic failure fixture")
        error.receipt = receipt
        class FailingTools(FixtureTools):
            async def retrieve(self, query, **kwargs):
                self.calls.append((query, kwargs))
                await store.save(receipt.model_dump(mode="json"), key="retrieval/error.json")
                raise error
        tools = FailingTools(None)
        result = await worker(store, tools, llm)(request())
        assert isinstance(result, ResultEnvelope)
        assert result.task_id == request().task_id and result.outcome == "permanent_error"
        assert result.error.code.value == code and result.error.retriable is False
        assert result.result_ref is None and not llm.calls and len(tools.calls) == 1
        assert "retrieval/error.json" in store.saved_keys and error.receipt == receipt
    asyncio.run(scenario())


@pytest.mark.parametrize("overrides", [
    {"query_text": "An unrelated query was executed."},
    {"corpus_version": "wrong-corpus-version"},
    {"filters": {"company_id": "other", "domain": DOMAIN.value, "as_of_date": "2026-01-01"}},
    {"filters": {"company_id": "acme", "domain": "Energy", "as_of_date": "2026-01-01"}},
    {"filters": {"company_id": "acme", "domain": DOMAIN.value, "as_of_date": "2026-01-02"}},
])
def test_retrieval_receipt_must_match_actual_query_company_domain_and_corpus(tmp_path, overrides):
    async def scenario():
        store = VerifiedJsonStore(tmp_path)
        source, evidence = await stored_source(store)
        tools = FixtureTools(retrieval_result(source=source, evidence=evidence), receipt_overrides=overrides)
        llm = FixtureLLM()
        with pytest.raises(ValueError, match="RETRIEVAL_RECEIPT_SCOPE_MISMATCH"):
            await worker(store, tools, llm)(request())
        assert not llm.calls and len(tools.calls) == 1
    asyncio.run(scenario())


def test_web_fetched_pdf_cannot_replace_actual_document_rag_requirement(tmp_path):
    import httpx
    from types import SimpleNamespace
    from main.rag.neutral.search import LegacySearchProvider, WebSourceMetadata
    from tests.fixtures.v2.suhyeon.store import write_pdf
    async def scenario():
        pdf = write_pdf(tmp_path / "web-trial.pdf", ["Acme sensor fusion model and ten validation laboratory trials."])
        store = VerifiedJsonStore(tmp_path / "store")
        class Backend:
            def search(self, queries, **kwargs):
                return [SimpleNamespace(url="https://example.test/acme.pdf", title="Acme real fixture PDF", content="Discovery snippet")]
        client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, content=pdf.read_bytes(), headers={"content-type": "application/pdf"})))
        service = LegacySearchProvider(backend=Backend(), store=store, company_id="acme", company_aliases=["Acme"],
                                       domain=DOMAIN, client=client,
                                       classify_source=lambda hit: WebSourceMetadata(source_type="institution"))
        class WebTools(FixtureTools):
            async def search(self, *args, **kwargs):
                return await service.search(*args, **kwargs)
            async def fetch(self, hit):
                return await service.fetch(hit)
        llm = FixtureLLM()
        async with client:
            result = await worker(store, WebTools(retrieval_result(outcome="empty")), llm,
                                  include_web=True, max_queries=1)(request(query_hints=["Acme trial"]))
        assert result.claims and result.evidence_ids and len(llm.calls) == 1
        assert any(gap.severity == "critical" and "실제 PDF 근거" in gap.question for gap in result.gaps)
        rag = [receipt for receipt in result.retrieval_receipts if receipt.provider == "deterministic-fixture"]
        web = [receipt for receipt in result.retrieval_receipts if receipt.provider == "legacy_web_search_http_body"]
        assert len(rag) == len(web) == 1 and rag[0].outcome == "empty" and web[0].outcome == "success"
        fetched_source = llm.calls[0][2]["sources"][0]
        assert fetched_source["original_file_id"].endswith(".pdf")
        assert llm.calls[0][2]["evidence"][0]["page_number"] == 1
    asyncio.run(scenario())
