"""Concrete composition of the seven owner services and the verified PDF index."""
from __future__ import annotations

import asyncio
import csv
from dataclasses import replace
from datetime import date
import json
import re
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from qdrant_client import QdrantClient

from main.agents.supervisor import contracts as c
from main.agents.eligibility.agent import EligibilityAgent
from main.agents.perspectives.technology import TechnologyWorker
from main.agents.perspectives.market import MarketAgent
from main.agents.perspectives.stakeholders import StakeholdersAgent
from main.agents.perspectives.domain import DomainAgent
from main.agents.market.web_search import OpenAIWebSearch
from main.agents.quality.claim_support import ClaimSupportVerifier
from main.agents.quality.rubrics import LLMClientJudge, RUBRIC_VERSION
from main.agents.quality.sufficiency import SufficiencyAssessor
from main.agents.quality.evaluator import ReportEvaluator
from main.agents.report.neutral_builder import NeutralWriter
from main.agents.report.neutral_publish import NeutralPublisher
from main.rag.neutral.embeddings import SentenceTransformerEmbeddings
from main.rag.neutral.ingestion import ingest_documents, legacy_corpus_documents
from main.rag.neutral.retrieval import NeutralRetriever, CorpusDomainUnavailableError, IndexMismatchError
from main.rag.neutral.search import LegacySearchProvider, WebSourceMetadata
from main.rag.neutral.tools import NeutralResearchTools, StoredEvidenceResolver, verify_acquired_evidence
from main.storage.artifacts import FileArtifactStore


def _company(name, url):
    value = c.RunInput(company_name=name, official_url=url,
        domain=c.Domain.PHYSICAL_AI_ROBOTICS, as_of_date=date(2000, 1, 1), corpus_manifest=[])
    return c.canonical_hash({"company_name": value.company_name, "official_url": str(value.official_url)})


def _documents(runtime, corpus_manifest):
    registry_path = runtime.repo_root / "docs/data/base/raw/thevc_startups_30_updated.csv"
    with registry_path.open(encoding="utf-8-sig", newline="") as stream:
        registry = {row["company_id"]: {"name": row["기업명"], "url": row["홈페이지"],
                    "canonical": _company(row["기업명"], row["홈페이지"])} for row in csv.DictReader(stream)}
    requested = {Path(item.file_path).resolve(): item for item in corpus_manifest}
    documents = {}
    aliases = {key: (row["name"],) for key, row in registry.items()}
    for directory in {path.parent.parent if path.parent.name == "raw" else path.parent for path in requested}:
        for original in legacy_corpus_documents(directory, company_aliases=aliases):
            path = original.path.resolve()
            if path not in requested:
                continue
            declared = requested[path]
            # Ownership is declared by the source manifest; CSV IDs are mapped
            # to the same name/URL fingerprint the Supervisor initializes.
            documents[path] = replace(original,
                company_ids=tuple(registry[key]["canonical"] for key in original.company_ids),
                expected_sha256=declared.sha256, expected_page_count=declared.page_count)
    if set(documents) != set(requested):
        raise CorpusDomainUnavailableError("Configured PDFs need verified original source metadata")
    return tuple(documents[path] for path in sorted(documents)), registry


async def _research(runtime, store, corpus_manifest, *, require_existing):
    documents, registry = _documents(runtime, corpus_manifest)
    control = runtime.run_config
    embedding = SentenceTransformerEmbeddings(control.embedding_model,
        revision=control.embedding_revision, normalize=True)
    corpus = await ingest_documents(documents, store=store, corpus_version=control.corpus_version,
        max_tokens=control.chunk_tokens, overlap_tokens=control.chunk_overlap_tokens,
        count_tokens=(embedding.count_tokens,))
    client = QdrantClient(url=runtime.qdrant_url,
        api_key=runtime.credentials.get("QDRANT_API_KEY"), timeout=control.tool_timeout_seconds)
    try:
        if require_existing and not await asyncio.to_thread(client.collection_exists, runtime.qdrant_collection):
            raise IndexMismatchError("Ingest the configured collection before running")
        retriever = NeutralRetriever(corpus=corpus, embedding=embedding, store=store, client=client,
            index_revision=control.index_version, collection_name=runtime.qdrant_collection)
        await retriever.initialize()
        return retriever, documents, registry
    except BaseException:
        await asyncio.to_thread(client.close)
        raise


async def ingest_production_corpus(*, runtime, store, corpus_manifest):
    retriever, _, _ = await _research(runtime, store, corpus_manifest, require_existing=False)
    try:
        control = runtime.run_config
        manifest = {"status": "completed", "normalized": True,
            "corpus_hash": c.canonical_hash([item.model_dump(mode="json") for item in corpus_manifest]),
            "embedding_model": control.embedding_model, "embedding_revision": control.embedding_revision,
            "embedding_dimension": control.embedding_dimension, "index_version": control.index_version,
            "collection": runtime.qdrant_collection, "retriever_index_hash": retriever.index_hash}
        await FileArtifactStore(runtime.index_manifest_path.parent).save(manifest,
            key=runtime.index_manifest_path.name)
        return {**manifest, "total_pages": retriever.corpus.total_pages}
    finally:
        await asyncio.to_thread(retriever.client.close)


async def evaluate_production_retrieval(*, runtime, store, corpus_manifest, dataset):
    from .services import evaluate_retrieval
    retriever, _, registry = await _research(runtime, store, corpus_manifest, require_existing=True)
    async def retrieve(*, query, top_k):
        owner = str(query["company_id"])
        company_id = registry.get(owner, {}).get("canonical", owner)
        result = await retriever.retrieve(query["query"], company_id=company_id,
            domain=c.Domain(query["domain"]), as_of_date=date.fromisoformat(query["as_of_date"]), top_k=top_k)
        return result.model_copy(update={"receipt": result.receipt.model_copy(update={"query_id": query["query_id"]})})
    try:
        return await evaluate_retrieval(SimpleNamespace(retrieve=retrieve, mock=False), dataset)
    finally:
        await asyncio.to_thread(retriever.client.close)


class RunEvidenceResolver(StoredEvidenceResolver):
    """Persist actual ArtifactRefs and the acquired PDF budget for resume."""
    KEY = "runtime/evidence-registry.json"

    def __init__(self, *, retriever, provider, **kwargs):
        super().__init__(**kwargs)
        self.retriever, self.provider = retriever, provider

    async def persist(self):
        await self.store.save({"company_id": self.company_id, "domain": self.domain.value,
            "source_refs": {key: ref.model_dump(mode="json") for key, ref in self.source_refs.items()},
            "evidence_refs": {key: ref.model_dump(mode="json") for key, ref in self.evidence_refs.items()},
            "receipt_refs": {key: ref.model_dump(mode="json") for key, ref in self.retriever.receipt_refs.items()},
            "pdf_pages": self.provider._pdf_pages, "pdf_hashes": sorted(self.provider._pdf_hashes)}, key=self.KEY)

    async def record(self, sources, evidence):
        await super().record(sources, evidence)
        await self.persist()


async def create_production_services(*, runtime, store, llm, run_input):
    from .services import ServiceBundle, ServiceConfigurationError
    run_input = c.RunInput.model_validate(run_input)
    control = runtime.run_config
    if control.rubric_version != RUBRIC_VERSION or runtime.search_provider != "openai":
        raise ServiceConfigurationError("Production services require neutral-quality-1.0 and search_provider=openai")
    # Check saved originals before re-ingesting: recreating the corpus must
    # never overwrite corruption and turn a broken checkpoint into approval.
    saved = None
    if await asyncio.to_thread(store._exists, RunEvidenceResolver.KEY):
        saved = json.loads(await asyncio.to_thread(store._read, RunEvidenceResolver.KEY))
        if saved["company_id"] != _company(run_input.company_name, str(run_input.official_url)) or saved["domain"] != run_input.domain.value:
            raise ValueError("Saved evidence registry identity mismatch")
        for field, model, identifier in (("source_refs", c.Source, "source_id"),
                ("evidence_refs", c.Evidence, "evidence_id"), ("receipt_refs", c.RetrievalReceipt, "query_id")):
            for key, raw in saved[field].items():
                item = model.model_validate(await store.load(c.ArtifactRef.model_validate(raw)))
                if getattr(item, identifier) != key:
                    raise ValueError("Saved evidence registry key mismatch")
                if field == "source_refs":
                    await store.verify(item.content_ref)
        if not isinstance(saved["pdf_hashes"], list) or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in saved["pdf_hashes"]):
            raise ValueError("Saved PDF hash budget is invalid")
    retriever, documents, registry = await _research(runtime, store, run_input.corpus_manifest, require_existing=True)
    provider = None
    backend = None
    async def close():
        try:
            if provider is not None:
                await provider.aclose()
            client = getattr(backend, "_client", None)
            if client is not None:
                await asyncio.to_thread(client.close)
        finally:
            await asyncio.to_thread(retriever.client.close)
    try:
        company_id = _company(run_input.company_name, str(run_input.official_url))
        if not any(row["canonical"] == company_id for row in registry.values()):
            raise CorpusDomainUnavailableError("Input name and official URL must match the prepared company registry")
        retriever.corpus.ensure_available(company_id, run_input.domain)
        known = {doc.original_url: doc for doc in documents if doc.original_url}
        def classify(hit):
            original = known.get(str(hit.url))
            if original is not None:
                return WebSourceMetadata(source_type=original.source_type, publisher=original.publisher,
                    publisher_group=original.publisher_group, published_at=original.published_at,
                    corporate_controlled=original.corporate_controlled)
            # An unclassified URL cannot become independent evidence based on
            # its hostname alone. Its date and independence remain unverified.
            host = urlsplit(str(hit.url)).hostname
            return WebSourceMetadata(source_type="news", publisher=host,
                publisher_group=host, corporate_controlled=True)
        backend = OpenAIWebSearch(model_name=control.research_model, max_retries=0,
            timeout_seconds=control.tool_timeout_seconds)
        provider = LegacySearchProvider(backend=backend, store=store, company_id=company_id,
            company_aliases=[run_input.company_name], domain=run_input.domain, classify_source=classify,
            timeout_seconds=control.tool_timeout_seconds, initial_pdf_pages=retriever.corpus.total_pages)
        source_refs, evidence_refs = dict(retriever.source_refs), {}
        if saved is not None:
            restored = {}
            for field in ("source_refs", "evidence_refs", "receipt_refs"):
                restored[field] = {key: c.ArtifactRef.model_validate(ref) for key, ref in saved[field].items()}
                for ref in restored[field].values():
                    await store.verify(ref)
            source_refs.update(restored["source_refs"])
            evidence_refs.update(restored["evidence_refs"])
            retriever.receipt_refs.update(restored["receipt_refs"])
            pages = saved["pdf_pages"]
            if type(pages) is not int or not retriever.corpus.total_pages <= pages <= 200:
                raise ValueError("Saved PDF budget is invalid")
            provider._pdf_pages = pages
            provider._pdf_hashes = set(saved["pdf_hashes"])
        for source in retriever.sources.values():
            content = await store.load(source.content_ref)
            provider._pdf_hashes.add(content["original_pdf_sha256"])
        resolver = RunEvidenceResolver(store=store, company_id=company_id, domain=run_input.domain,
            source_refs=source_refs, evidence_refs=evidence_refs, retriever=retriever, provider=provider)
        await resolver.resolve(list(evidence_refs))
        tools = NeutralResearchTools(retriever=retriever, search_provider=provider)
        async def excerpt_loader(source, evidence):
            evidence, source = c.Evidence.model_validate(evidence), c.Source.model_validate(source)
            await verify_acquired_evidence(store, evidence, source, company_id=company_id, domain=run_input.domain)
            content = await store.load(source.content_ref)
            return next(page["text"] for page in content["pages"] if page["page_number"] == evidence.page_number) if evidence.page_number else content["text"]
        async def receipt_validator(receipt):
            receipt = c.RetrievalReceipt.model_validate(receipt)
            ref = retriever.receipt_refs.get(receipt.query_id)
            if ref is None:
                return False
            actual = c.RetrievalReceipt.model_validate(await store.load(ref))
            # TechnologyWorker binds queries_hash to the full task query set.
            return actual.model_dump(exclude={"queries_hash"}) == receipt.model_dump(exclude={"queries_hash"})
        judge = LLMClientJudge(client=llm, model=control.judge_model)
        verifier = ClaimSupportVerifier(judge=judge, excerpt_loader=excerpt_loader,
            timeout_seconds=control.llm_timeout_seconds)
        assessor = SufficiencyAssessor(verifier=verifier, judge=judge, save_claim_support=store.save,
            receipt_validator=receipt_validator, company_id=company_id, corpus_version=control.corpus_version,
            index_version=control.index_version, timeout_seconds=control.llm_timeout_seconds)
        common = {"tools": tools, "llm": llm, "domain": run_input.domain, "model": control.research_model,
                  "store": store, "resolver": resolver, "timeout_seconds": control.tool_timeout_seconds}
        identity = {"company_name": run_input.company_name, "official_url": str(run_input.official_url), "company_id": company_id}
        workers = {"eligibility": EligibilityAgent(**common),
            "technology_maturity": TechnologyWorker(tools=tools, store=store, llm=llm,
                model=control.research_model, company_id=company_id, company_name=run_input.company_name,
                domain=run_input.domain, corpus_version=control.corpus_version, resolver=resolver,
                max_queries=control.max_queries_per_view),
            "market": MarketAgent(**common, **identity), "stakeholders": StakeholdersAgent(**common, **identity),
            "domain_application": DomainAgent(**common, **identity),
            "writer": NeutralWriter(llm=llm, model=control.report_model),
            "evaluator": ReportEvaluator(verifier=verifier, judge=judge, company_id=company_id,
                judge_model=control.judge_model, timeout_seconds=control.llm_timeout_seconds)}
        def track(worker):
            async def invoke(request):
                try:
                    return await worker(request)
                finally:
                    await resolver.persist()
            return invoke
        for name in ("eligibility", *(p.value for p in c.PERSPECTIVES)):
            workers[name] = track(workers[name])
        publisher = NeutralPublisher(store=store, evidence_resolver=resolver,
            artifact_root=store.root, font_path=runtime.report_font_path)
        await resolver.persist()
        return ServiceBundle(workers=workers, evidence_resolver=resolver, assess_evidence=assessor,
            publish_worker=publisher, mock=getattr(llm, "mock", False), close=close)
    except BaseException:
        await close()
        raise
