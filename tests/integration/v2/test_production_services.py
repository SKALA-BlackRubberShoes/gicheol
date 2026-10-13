"""Production wiring with real PDF/files/Qdrant and explicit external fixtures."""
import asyncio
import csv
from dataclasses import replace
from datetime import date
import json
from pathlib import Path
import shutil

import pytest
from qdrant_client import QdrantClient
from reportlab.pdfgen.canvas import Canvas
import yaml

from main.agents.supervisor import contracts as c
from main.runtime.neutral import services
from main.runtime.neutral.config import inspect_corpus, load_runtime_config
from main.storage.artifacts import FileArtifactStore
from main.storage.artifacts import ArtifactIntegrityError
from main.rag.neutral.ingestion import CorpusIngestionError


class Embedding:
    model_id = "BAAI/bge-m3"
    revision = "a" * 40
    dimension = 1024
    normalize = True

    def __init__(self, *args, **kwargs):
        pass

    def count_tokens(self, text, **kwargs):
        return len(text.split())

    async def embed_documents(self, texts):
        return [[1.0] + [0.0] * 1023 for _ in texts]

    async def embed_query(self, text):
        return [1.0] + [0.0] * 1023


class Models:
    mock = True

    async def generate(self, *, model, system_prompt, payload, response_model):
        properties = response_model.model_fields
        if "support_status" in properties:
            value = {"support_status": "supported", "reason": "Deterministic fixture",
                     "supported_evidence_ids": payload["claim"]["evidence_ids"]}
        elif "semantic_pass" in properties:
            value = {"semantic_pass": True, "reason": "Deterministic fixture", "gaps": []}
        elif "criterion_scores" in properties:
            value = {"criterion_scores": {x.value: 4 for x in c.Criterion}, "findings": []}
        elif "revision" in properties:
            value = payload["draft"]
        else:
            items = payload["evidence"][:2]
            claims = [{"claim_id": "fixture-tech-" + str(i), "perspective": "technology_maturity",
                       "text": item["quote"], "kind": "fact", "evidence_ids": [item["evidence_id"]],
                       "limitations": ["Synthetic PDF fixture only"]} for i, item in enumerate(items)]
            value = {"claims": claims, "answered_questions": {
                "tech.principle": [claims[0]["claim_id"]], "tech.validation": [claims[-1]["claim_id"]]},
                     "limitations": ["Synthetic PDF fixture only"]}
        return response_model.model_validate(value)


def deployment(tmp_path):
    source = tmp_path / "docs/data/technology_competition"
    raw = source / "raw"
    raw.mkdir(parents=True)
    rows = []
    for i, text in enumerate([
        "Alpha uses a learned perception model. Alpha is unlisted at Seed stage and has not exited.",
        "Alpha achieved 90 percent accuracy in its documented indoor test. Deployment limits remain.",
    ]):
        path = raw / f"source-{i}.pdf"
        canvas = Canvas(str(path))
        canvas.drawString(40, 700, text)
        canvas.save()
        rows.append({"file_name": "raw/" + path.name, "document_id": f"fixture-source-{i}",
                     "company_ids": [14], "company_names": {"14": ["Alpha"]},
                     "source_type": "institution", "publisher": f"Fixture Institution {i}",
                     "title": f"Alpha evidence {i}", "published_date": "2026-01-01"})
    (source / "sources.json").write_text(json.dumps(rows))
    registry = tmp_path / "docs/data/base/raw"
    registry.mkdir(parents=True)
    with (registry / "thevc_startups_30_updated.csv").open("w") as file:
        writer = csv.DictWriter(file, fieldnames=["company_id", "기업명", "홈페이지"])
        writer.writeheader()
        writer.writerow({"company_id": "14", "기업명": "Alpha", "홈페이지": "https://alpha.example/"})
    values = {"run_config": {"research_model": "fixture-research", "report_model": "fixture-report",
                             "judge_model": "fixture-judge", "rubric_version": "neutral-quality-1.0",
                             "embedding_model": "BAAI/bge-m3", "embedding_revision": "a" * 40,
                             "embedding_dimension": 1024, "index_version": "fixture-v1"},
              "corpus_paths": [str(raw / "*.pdf")], "corpus_domains": ["Physical AI / Robotics"],
              "qdrant_url": "http://fixture.invalid", "qdrant_collection": "neutral-test",
              "search_provider": "openai", "credential_env_vars": []}
    path = tmp_path / "runtime.yaml"
    path.write_text(yaml.safe_dump(values))
    runtime = load_runtime_config(path, repo_root=tmp_path, environ={})
    run_input = c.RunInput(company_name="Alpha", official_url="https://alpha.example/",
                          domain="Physical AI / Robotics", as_of_date="2026-10-13",
                          corpus_manifest=inspect_corpus(runtime))
    return runtime, run_input


@pytest.mark.asyncio
async def test_default_factory_wires_real_workers_and_canonical_rag(tmp_path, monkeypatch):
    factory = getattr(services, "create_production_services", None)
    assert callable(factory), "Default production service assembly is missing"
    from main.runtime.neutral import production
    runtime, run_input = deployment(tmp_path)
    monkeypatch.setattr(production, "SentenceTransformerEmbeddings", Embedding)
    monkeypatch.setattr(production, "QdrantClient", lambda **kwargs: QdrantClient(path=str(tmp_path / "index")))
    await production.ingest_production_corpus(runtime=runtime,
        store=FileArtifactStore(tmp_path / "ingest"), corpus_manifest=run_input.corpus_manifest)
    store = FileArtifactStore(tmp_path / "run")
    bundle = await services.load_services(runtime, store, Models(), run_input=run_input)
    try:
        bundle.validate()
        company_id = c.canonical_hash({"company_name": "Alpha", "official_url": "https://alpha.example/"})
        result = await bundle.workers["technology_maturity"](c.ResearchRequest(
            task_id="factory-tech", company_id=company_id, perspective="technology_maturity",
            as_of_date=date(2026, 10, 13), attempt=1, corpus_version="1.0"))
        assert result.evidence_ids
        evidence = await bundle.evidence_resolver.resolve(result.evidence_ids)
        assert {item.company_id for item in evidence.evidence} == {company_id}
        assert all(item.page_number == 1 for item in evidence.evidence)
        assert all(receipt.filters["company_id"] == company_id for receipt in result.retrieval_receipts)
        assert set(bundle.workers) == {"eligibility", "technology_maturity", "market", "stakeholders",
                                      "domain_application", "writer", "evaluator"}
        assert bundle.mock is True
        support = await bundle.assess_evidence.verifier.verify(result.claims[0],
            evidence=evidence.evidence, sources=evidence.sources, company_id=company_id,
            as_of_date=run_input.as_of_date)
        assert support.support_status == "supported"
    finally:
        await bundle.close()
    # Reconstruct the real factory and Qdrant from disk, as CLI resume does.
    rebuilt = await services.load_services(runtime, store, Models(), run_input=run_input)
    try:
        restored = await rebuilt.evidence_resolver.resolve(result.evidence_ids)
        assert restored == evidence
        assert rebuilt.evidence_resolver.provider._pdf_pages == 2
        assert len(rebuilt.evidence_resolver.provider._pdf_hashes) == 2
        assert await rebuilt.assess_evidence.receipt_validator(result.retrieval_receipts[0])
        changed = result.retrieval_receipts[0].model_copy(update={"query_text": "forged receipt"})
        assert not await rebuilt.assess_evidence.receipt_validator(changed)
        item = restored.evidence[0]
        original = next(source for source in restored.sources if source.source_id == item.source_id)
        assert item.quote in await rebuilt.assess_evidence.verifier.excerpt_loader(original, item)
        path = store.root / original.content_ref.relative_path
        path.write_bytes(path.read_bytes() + b"tampered")
        with pytest.raises(ArtifactIntegrityError):
            await rebuilt.evidence_resolver.resolve(result.evidence_ids)
    finally:
        await rebuilt.close()
    with pytest.raises(ArtifactIntegrityError):
        await services.load_services(runtime, store, Models(), run_input=run_input)


@pytest.mark.asyncio
async def test_factory_rejects_other_company_and_missing_index(tmp_path, monkeypatch):
    from main.runtime.neutral import production
    from main.rag.neutral.retrieval import CorpusDomainUnavailableError, IndexMismatchError
    runtime, run_input = deployment(tmp_path)
    monkeypatch.setattr(production, "SentenceTransformerEmbeddings", Embedding)
    monkeypatch.setattr(production, "QdrantClient", lambda **kwargs: QdrantClient(path=str(tmp_path / "index")))
    with pytest.raises(IndexMismatchError):
        await services.load_services(runtime, FileArtifactStore(tmp_path / "missing"), Models(), run_input=run_input)
    await production.ingest_production_corpus(runtime=runtime,
        store=FileArtifactStore(tmp_path / "ingest"), corpus_manifest=run_input.corpus_manifest)
    other = run_input.model_copy(update={"company_name": "Beta"})
    with pytest.raises(CorpusDomainUnavailableError):
        await services.load_services(runtime, FileArtifactStore(tmp_path / "other"), Models(), run_input=other)
    # A failed assembly closes the real client; the original company can run.
    bundle = await services.load_services(runtime, FileArtifactStore(tmp_path / "correct"), Models(), run_input=run_input)
    await bundle.close()


@pytest.mark.asyncio
async def test_cli_ingest_and_retrieval_use_actual_production_adapters(tmp_path, monkeypatch, capsys):
    from main import neutral_eval as cli
    from main.runtime.neutral import production
    from types import SimpleNamespace
    runtime, run_input = deployment(tmp_path)
    monkeypatch.setattr(production, "SentenceTransformerEmbeddings", Embedding)
    monkeypatch.setattr(production, "QdrantClient", lambda **kwargs: QdrantClient(path=str(tmp_path / "index")))
    monkeypatch.setattr(cli, "load_runtime_config", lambda *args, **kwargs: runtime)
    monkeypatch.setattr(cli, "preflight", lambda *args, **kwargs: SimpleNamespace(
        config=runtime, corpus_manifest=run_input.corpus_manifest))
    assert await cli.dispatch(cli.parser().parse_args(["ingest"])) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "completed"
    gold = {"queries": [{"query_id": f"q{i}", "query": "Alpha indoor learned perception",
        "perspective": c.PERSPECTIVES[i // 5].value, "language": "ko" if i < 10 else "en",
        "company_id": "14", "domain": run_input.domain.value, "as_of_date": "2026-10-13",
        "relevant_source_ids": ["fixture-source-0", "fixture-source-1"]} for i in range(20)]}
    path = tmp_path / "gold.json"
    path.write_text(json.dumps(gold))
    assert await cli.dispatch(cli.parser().parse_args(["evaluate-retrieval", "--dataset", str(path)])) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["hit_rate_at_5"] == result["mrr_at_5"] == 1.0
    assert result["query_count"] == 20
    # This metric uses real retrieval but synthetic external embeddings.
    # The test asserts adapter wiring, not a live retrieval acceptance score.


@pytest.mark.parametrize("declared_name", ["Alpha", "Beta"])
def test_external_corpus_identity_uses_authoritative_company_registry(tmp_path, declared_name):
    from main.runtime.neutral import production
    runtime, run_input = deployment(tmp_path)
    source = tmp_path / "docs/data/technology_competition"
    external = tmp_path / "external-corpus"
    shutil.copytree(source, external)
    rows = json.loads((external / "sources.json").read_text())
    for row in rows:
        row["company_names"]["14"] = [declared_name]
    (external / "sources.json").write_text(json.dumps(rows))
    runtime = replace(runtime, corpus_paths=(str(external / "raw/*.pdf"),))
    manifest = inspect_corpus(runtime)
    if declared_name == "Beta":
        with pytest.raises(CorpusIngestionError, match="identity differs"):
            production._documents(runtime, manifest)
    else:
        documents, registry = production._documents(runtime, manifest)
        assert all(document.company_names == ("Alpha",) for document in documents)
        assert all(document.company_ids == (registry["14"]["canonical"],) for document in documents)
