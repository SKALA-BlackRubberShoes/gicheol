"""Legacy snippets discover URLs; HTTP body acquisition creates evidence."""
import asyncio
from types import SimpleNamespace

import httpx
import pytest

from main.rag.neutral.search import LegacySearchProvider, SourceInaccessibleError, WebSourceMetadata
from tests.fixtures.v2.suhyeon.store import AS_OF, DOMAIN, VerifiedJsonStore, write_pdf


class Backend:
    def __init__(self, rows=None, fail=False):
        self.rows, self.fail, self.queries = rows or [], fail, []

    def search(self, queries, *, max_results_per_query):
        self.queries.append((queries, max_results_per_query))
        if self.fail:
            raise RuntimeError("simulated search outage")
        return self.rows


def row(url="https://example.test/acme", content="Acme invented snippet with perfect results"):
    return SimpleNamespace(url=url, title="Acme trial", content=content)


def provider(store, backend, handler, **kwargs):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return LegacySearchProvider(backend=backend, store=store, company_id="acme", company_aliases=["Acme"],
                                domain=DOMAIN, classify_source=lambda hit: WebSourceMetadata(source_type="institution", publisher="Institute"),
                                client=client, **kwargs), client


def test_search_deduplicates_and_bounds_safe_company_scoped_discovery(tmp_path):
    async def scenario():
        backend = Backend([row("https://example.test/acme#first"), row("https://example.test/acme#second"),
                           row("file:///secret"), row("https://user@example.test/acme")])
        service, client = provider(VerifiedJsonStore(tmp_path), backend, lambda request: httpx.Response(200))
        async with client:
            result = await service.search("trial", company_id="acme", domain=DOMAIN, as_of_date=AS_OF, top_k=5)
        assert len(result.hits) == 1 and result.outcome == "success"
        assert str(result.hits[0].url) == "https://example.test/acme"
        assert "acme trial" in backend.queries[0][0][0] and "2026-01-01" in backend.queries[0][0][0]
        assert not service.store.saved_keys
    asyncio.run(scenario())


@pytest.mark.parametrize("fail,outcome", [(False, "empty"), (True, "error")])
def test_search_empty_and_error_are_distinct(tmp_path, fail, outcome):
    async def scenario():
        service, client = provider(VerifiedJsonStore(tmp_path), Backend(fail=fail), lambda req: httpx.Response(200))
        async with client:
            result = await service.search("trial", company_id="acme", domain=DOMAIN, as_of_date=AS_OF)
        assert result.hits == [] and result.outcome == outcome
    asyncio.run(scenario())


def test_fetch_saves_actual_html_body_and_ignores_snippet_and_hidden_script(tmp_path):
    async def scenario():
        store = VerifiedJsonStore(tmp_path)
        body = "<html><nav>Menu</nav><article>Acme trial used ten laboratory attempts.</article><script>perfect results</script></html>"
        service, client = provider(store, Backend([row()]), lambda req: httpx.Response(200, text=body, headers={"content-type": "text/html"}))
        async with client:
            result = await service.search("trial", company_id="acme", domain=DOMAIN, as_of_date=AS_OF)
            fetched = await service.fetch(result.hits[0])
        assert fetched.text == "Acme trial used ten laboratory attempts."
        content = await store.load(fetched.source.content_ref)
        assert content["text"] == fetched.text and content["body_verified"]
        assert "perfect" not in content["text"] and "snippet" not in content["text"]
        assert fetched.source.published_at is None
    asyncio.run(scenario())


def test_fetch_real_pdf_preserves_actual_page_numbers(tmp_path):
    async def scenario():
        path = write_pdf(tmp_path / "trial.pdf", ["Acme sensor fusion model", "Acme validation ten laboratory trials"])
        store = VerifiedJsonStore(tmp_path / "store")
        service, client = provider(store, Backend([row()]), lambda req: httpx.Response(200, content=path.read_bytes(), headers={"content-type": "application/pdf"}))
        async with client:
            result = await service.search("trial", company_id="acme", domain=DOMAIN, as_of_date=AS_OF)
            fetched = await service.fetch(result.hits[0])
        content = await store.load(fetched.source.content_ref)
        assert [page["page_number"] for page in content["pages"]] == [1, 2]
        assert "sensor fusion" in content["pages"][0]["text"]
        assert "ten laboratory" in content["pages"][1]["text"]
    asyncio.run(scenario())


@pytest.mark.parametrize("status,body,content_type,error", [
    (404, "missing", "text/plain", "SOURCE_INACCESSIBLE"),
    (200, "Other company trial", "text/plain", "BODY_COMPANY_MISMATCH"),
    (200, "", "text/plain", "EMPTY_SOURCE_BODY"),
    (200, "Acme trial", "application/octet-stream", "UNSUPPORTED_BODY_TYPE"),
    (200, "%PDF-invalid", "application/pdf", "PDF_PARSE_FAILED"),
])
def test_discovered_snippet_cannot_replace_unavailable_or_invalid_body(tmp_path, status, body, content_type, error):
    async def scenario():
        store = VerifiedJsonStore(tmp_path)
        service, client = provider(store, Backend([row()]), lambda req: httpx.Response(status, text=body, headers={"content-type": content_type}))
        async with client:
            result = await service.search("trial", company_id="acme", domain=DOMAIN, as_of_date=AS_OF)
            with pytest.raises(SourceInaccessibleError, match=error):
                await service.fetch(result.hits[0])
        assert not store.saved_keys
    asyncio.run(scenario())


def test_fetch_requires_provider_owned_hit_and_enforces_body_budget(tmp_path):
    async def scenario():
        service, client = provider(VerifiedJsonStore(tmp_path), Backend([row()]), lambda req: httpx.Response(200, text="Acme body exceeds budget", headers={"content-type": "text/plain"}), max_bytes=5)
        async with client:
            result = await service.search("trial", company_id="acme", domain=DOMAIN, as_of_date=AS_OF)
            with pytest.raises(ValueError, match="discovered"):
                await service.fetch(result.hits[0].model_copy(update={"title": "Forged hit"}))
            with pytest.raises(SourceInaccessibleError, match="SOURCE_BODY_TOO_LARGE"):
                await service.fetch(result.hits[0])
    asyncio.run(scenario())


def test_web_pdf_cumulative_pool_includes_existing_199_pages(tmp_path):
    async def scenario():
        pdf = write_pdf(tmp_path / "two-pages.pdf", ["Acme trial page one", "Acme trial page two"])
        store = VerifiedJsonStore(tmp_path / "store")
        service, client = provider(store, Backend([row()]), lambda req: httpx.Response(200, content=pdf.read_bytes(), headers={"content-type": "application/pdf"}), initial_pdf_pages=199)
        async with client:
            result = await service.search("trial", company_id="acme", domain=DOMAIN, as_of_date=AS_OF)
            with pytest.raises(SourceInaccessibleError, match="PDF_POOL_OVER_BUDGET"):
                await service.fetch(result.hits[0])
        assert not store.saved_keys
    asyncio.run(scenario())


def test_same_url_changed_body_preserves_original_reference_and_rejects_source_reuse(tmp_path):
    from main.agents.supervisor.contracts import Evidence
    from main.rag.neutral.tools import StoredEvidenceResolver
    async def scenario():
        store = VerifiedJsonStore(tmp_path)
        response_bodies = iter(["Acme original body.", "Acme different body."])
        service, client = provider(store, Backend([row()]), lambda req: httpx.Response(200, text=next(response_bodies), headers={"content-type": "text/plain"}))
        async with client:
            result = await service.search("trial", company_id="acme", domain=DOMAIN, as_of_date=AS_OF)
            first, second = await service.fetch(result.hits[0]), await service.fetch(result.hits[0])
        assert first.source.source_id == second.source.source_id
        assert first.source.content_ref != second.source.content_ref
        assert (await store.load(first.source.content_ref))["text"] == "Acme original body."
        resolver = StoredEvidenceResolver(store=store, company_id="acme", domain=DOMAIN)
        def evidence(fetched, suffix):
            return Evidence(evidence_id="web-evidence-" + suffix, source_id=fetched.source.source_id,
                            company_id="acme", chunk_id=f"{fetched.source.source_id}:0-{len(fetched.text)}",
                            quote=fetched.text)
        await resolver.record([first.source], [evidence(first, "first")])
        with pytest.raises(ValueError, match="SOURCE_ID_CONTENT_CHANGED"):
            await resolver.record([second.source], [evidence(second, "second")])
        assert (await resolver.resolve(["web-evidence-first"])).sources == [first.source]
    asyncio.run(scenario())
