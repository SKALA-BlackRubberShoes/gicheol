"""Real web acquisition and persistent resolver integration for all four workers.

HTTP responses and structured model replies are deterministic. The legacy
search adapter, tool facade, body verification, resolver and file-byte-hashed
ArtifactStore are real implementations, including registry reconstruction.
"""
import asyncio
import base64
import hashlib
from datetime import date, datetime, timezone
from urllib.parse import urlsplit

import httpx
import pytest

from main.agents.eligibility.agent import EligibilityAgent
from main.agents.market.schemas import WebSearchResult
from main.agents.perspectives.domain import DomainAgent
from main.agents.perspectives.market import MarketAgent
from main.agents.perspectives.stakeholders import StakeholdersAgent
from main.agents.supervisor import contracts as c
from main.rag.neutral.search import LegacySearchProvider, WebSourceMetadata
from main.rag.neutral.tools import NeutralResearchTools, StoredEvidenceResolver
from tests.fixtures.v2.suhyeon.store import VerifiedJsonStore


DOMAIN = c.Domain.PHYSICAL_AI_ROBOTICS
AS_OF = date(2026, 10, 13)
NOW = datetime(2026, 10, 13, tzinfo=timezone.utc)
URLS = ("https://registry.example/acme", "https://institute.example/acme")
SNIPPET = "Acme invented a perfect outcome in this discovery-only snippet."


def original_body(index):
    # The target appears beyond both the old 2000-character excerpt and the
    # provider's bounded 20000-character FetchedContent.text prefix. Leading,
    # internal and trailing Unicode whitespace must not corrupt span offsets.
    prelude = "\u2003\t" + "독립 배경 λ — 🙂. \n" * 1900
    statements = (
        "\n\tAcme is unlisted as of 2026-10-13.\u2003\n"
        "  Acme current funding stage is Series B as of 2026-10-13.\n"
        "\tAcme has not completed an exit as of 2026-10-13.\u2003\n"
        f"Acme pilot {index} measured bounded warehouse demand and operating costs.\n"
        "Acme operators required training and retained safety responsibility.\n"
        "Acme warehouse deployment required calibrated sensors and tested lighting.\n\u2003\t"
    )
    body = prelude + statements
    assert body.index("Acme") > 20000
    return body


class DiscoveryBackend:
    def __init__(self):
        self.calls = []

    async def search(self, queries, *, max_results_per_query):
        self.calls.append((queries, max_results_per_query))
        return [WebSearchResult(query=queries[0], url=url,
            title=f"Acme original register {index}", content=SNIPPET)
            for index, url in enumerate(URLS)]


class EmptyRetriever:
    """Only PDF retrieval is substituted; every web source uses the real adapter."""
    async def retrieve(self, query, *, company_id, domain, as_of_date, top_k=5):
        assert top_k == 5
        receipt = c.RetrievalReceipt(query_id="empty-" + c.canonical_hash(query)[:24],
            query_text=query, queries_hash=c.canonical_hash([query]), provider="pdf",
            corpus_version="web-fixture-v1", index_version="empty-fixture-index",
            filters={"company_id": company_id, "domain": c.Domain(domain).value,
                "as_of_date": as_of_date.isoformat()}, ranked_hits=[], outcome="empty", ts=NOW)
        return c.RetrievalResult(sources=[], evidence=[], receipt=receipt)


class DeterministicLLM:
    def __init__(self):
        self.calls = []

    async def generate(self, *, model, system_prompt, payload, response_model):
        self.calls.append(response_model)
        if response_model is c.EligibilityResult:
            # The actual worker must derive eligibility from acquired proofs;
            # this model supplies no affirmative conditions or invented facts.
            return c.EligibilityResult(task_id=payload["task_id"], unlisted="unknown",
                funding_stage="unknown", exit_status="unknown", verdict="unknown",
                claims=[], gaps=[])
        assert response_model is c.PerspectiveResult
        request = c.ResearchRequest.model_validate(payload["request"])
        evidence = [c.Evidence.model_validate(item) for item in payload["evidence"]]
        chosen = list({item.source_id: item for item in evidence}.values())[:2]
        assert len(chosen) == 2
        claims = [c.Claim(claim_id=f"{request.perspective.value}-web-{index}",
            perspective=request.perspective, text=item.quote, kind="fact",
            evidence_ids=[item.evidence_id], limitations=["Synthetic integration fixture only."])
            for index, item in enumerate(chosen)]
        return c.PerspectiveResult(task_id=request.task_id,
            perspective=request.perspective, attempt=request.attempt, claims=claims,
            evidence_ids=[item.evidence_id for item in evidence],
            answered_questions={question: [claims[index].claim_id]
                for index, question in enumerate(c.REQUIRED_QUESTIONS[request.perspective])},
            gaps=[], limitations=["Synthetic integration fixture only."], retrieval_receipts=[])


async def web_run(root):
    store = VerifiedJsonStore(root)
    resolver = StoredEvidenceResolver(store=store, company_id="acme", domain=DOMAIN)
    backend = DiscoveryBackend()
    responses = {url: original_body(index).encode("utf-8") for index, url in enumerate(URLS)}
    http_calls = []

    def handle(request):
        http_calls.append(str(request.url))
        return httpx.Response(200, content=responses[str(request.url)],
            headers={"content-type": "text/plain; charset=utf-8"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    provider = LegacySearchProvider(backend=backend, store=store, company_id="acme",
        company_aliases=["Acme"], domain=DOMAIN, client=client,
        classify_source=lambda hit: WebSourceMetadata(source_type="institution",
            publisher="Original Register", publisher_group=urlsplit(str(hit.url)).hostname,
            published_at=AS_OF, corporate_controlled=False))
    tools = NeutralResearchTools(retriever=EmptyRetriever(), search_provider=provider)
    llm = DeterministicLLM()
    workers = {"eligibility": EligibilityAgent(tools=tools, llm=llm, domain=DOMAIN,
        model="deterministic-model", store=store, resolver=resolver)}
    worker_classes = {c.Perspective.MARKET: MarketAgent,
        c.Perspective.STAKEHOLDERS: StakeholdersAgent,
        c.Perspective.DOMAIN_APPLICATION: DomainAgent}
    for perspective, worker_class in worker_classes.items():
        workers[perspective.value] = worker_class(tools=tools, llm=llm, domain=DOMAIN,
            model="deterministic-model", store=store, resolver=resolver,
            company_id="acme", company_name="Acme", official_url="https://acme.example")
    results, evidence_ids = {}, {}
    async with client:
        results["eligibility"] = await workers["eligibility"](c.EligibilityRequest(
            task_id="web-eligibility", company_id="acme", company_name="Acme",
            official_url="https://acme.example", as_of_date=AS_OF, attempt=1))
        assert results["eligibility"].verdict == "eligible"
        evidence_ids["eligibility"] = list(dict.fromkeys(item_id
            for claim in results["eligibility"].claims for item_id in claim.evidence_ids))
        for perspective in worker_classes:
            result = await workers[perspective.value](c.ResearchRequest(
                task_id=f"web-{perspective.value}", company_id="acme",
                perspective=perspective, as_of_date=AS_OF, attempt=1,
                corpus_version="web-fixture-v1"))
            results[perspective.value] = result
            evidence_ids[perspective.value] = result.evidence_ids
            assert set(result.answered_questions) == set(c.REQUIRED_QUESTIONS[perspective])
            assert not any(gap.severity == c.Severity.CRITICAL for gap in result.gaps)

    assert len(llm.calls) == 4
    assert set(http_calls) == set(URLS)
    assert all(top_k == 5 for _, top_k in backend.calls)
    assert all(item_ids for item_ids in evidence_ids.values())
    for worker in workers.values():
        assert worker.source_refs is resolver.source_refs
        assert worker.evidence_refs is resolver.evidence_refs
    all_ids = list(dict.fromkeys(item_id for item_ids in evidence_ids.values() for item_id in item_ids))
    original_bundle = await resolver.resolve(all_ids)

    # Persist the actual ArtifactRefs in JSON, then rebuild both store and
    # resolver. Reusing live Python dictionaries would hide resume failures.
    registry_ref = await store.save({
        "source_refs": {item_id: ref.model_dump(mode="json") for item_id, ref in resolver.source_refs.items()},
        "evidence_refs": {item_id: ref.model_dump(mode="json") for item_id, ref in resolver.evidence_refs.items()},
    }, key="registry/web-evidence.json")
    resumed_store = VerifiedJsonStore(root)
    registry = await resumed_store.load(registry_ref)
    resumed = StoredEvidenceResolver(store=resumed_store, company_id="acme", domain=DOMAIN,
        source_refs={key: c.ArtifactRef.model_validate(value) for key, value in registry["source_refs"].items()},
        evidence_refs={key: c.ArtifactRef.model_validate(value) for key, value in registry["evidence_refs"].items()})
    assert resumed.source_refs is not resolver.source_refs
    assert resumed.evidence_refs is not resolver.evidence_refs
    assert await resumed.resolve(all_ids) == original_bundle
    return resumed, evidence_ids, responses


def test_all_four_real_web_workers_resolve_exact_body_spans_after_registry_reload(tmp_path):
    async def scenario():
        resumed, evidence_ids, responses = await web_run(tmp_path)
        for item_ids in evidence_ids.values():
            bundle = await resumed.resolve(item_ids)
            sources = {source.source_id: source for source in bundle.sources}
            for item in bundle.evidence:
                source = sources[item.source_id]
                body = await resumed.store.load(source.content_ref)
                start, end = map(int, item.chunk_id.rsplit(":", 1)[1].split("-"))
                assert item.chunk_id.startswith(source.source_id + ":")
                assert start > 20000 and end <= len(body["text"])
                assert body["text"][start:end] == item.quote
                assert "Acme" in item.quote and SNIPPET not in item.quote
                assert item.quote_hash == hashlib.sha256(item.quote.encode("utf-8")).hexdigest()
                original_bytes = base64.b64decode(body["original_body_base64"], validate=True)
                assert original_bytes == responses[str(source.canonical_url)]
                assert body["text"] == original_bytes.decode("utf-8").strip()
                assert body["original_body_sha256"] == hashlib.sha256(original_bytes).hexdigest()
                assert body["body_verified"] and body["company_ids"] == ["acme"]
                assert body["domain"] == DOMAIN.value
    asyncio.run(scenario())


@pytest.mark.parametrize("worker", ["eligibility", "market"])
@pytest.mark.parametrize("damage", ["body_bytes", "quote_with_new_hash", "span_with_new_artifact_hash"])
def test_real_resolver_rejects_tampered_resumed_worker_evidence(tmp_path, worker, damage):
    async def scenario():
        resumed, evidence_ids, _ = await web_run(tmp_path)
        item_id = evidence_ids[worker][0]
        bundle = await resumed.resolve([item_id])
        item, source = bundle.evidence[0], bundle.sources[0]
        if damage == "body_bytes":
            path = resumed.store.path(source.content_ref)
            path.write_bytes(path.read_bytes() + b"corrupt")
            error = "ARTIFACT_BYTES_HASH_MISMATCH"
        else:
            data = item.model_dump(mode="json")
            if damage == "quote_with_new_hash":
                data.update(quote=item.quote + " fabricated", quote_hash=None)
            else:
                start, end = map(int, item.chunk_id.rsplit(":", 1)[1].split("-"))
                data["chunk_id"] = f"{source.source_id}:{start + 1}-{end}"
            changed = c.Evidence.model_validate(data)
            resumed.evidence_refs[item_id] = await resumed.store.save(
                changed.model_dump(mode="json"), key=f"tampered/{item_id}.json")
            error = "WEB_LOCATION_MISMATCH"
        with pytest.raises(ValueError, match=error):
            await resumed.resolve([item_id])
    asyncio.run(scenario())
