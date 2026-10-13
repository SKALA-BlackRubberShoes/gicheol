"""Real pinned-model comparison; run explicitly, never during unit tests.

Example: python docs/v2/suhyeon/run-embedding-benchmark.py --cache /path/to/cache
  --work-directory /path/to/benchmark-work --model BAAI/bge-m3
Use a fresh work directory for a new measurement. Output records actual ranks,
source labels, index time, warmed query latency and sampled process peak RSS.
The evaluation owner is synthetic and invalid as production company evidence.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import platform
import statistics
import sys
import threading
import time
from dataclasses import replace
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from main.agents.supervisor.contracts import ArtifactRef, Domain, canonical_hash, canonical_json
from main.rag.neutral.embeddings import MODEL_SPECS, SentenceTransformerEmbeddings
from main.rag.neutral.ingestion import ingest_documents, legacy_corpus_documents
from main.rag.neutral.retrieval import NeutralRetriever, ranking_metrics


class BenchmarkArtifacts:
    """Actual atomic JSON files used solely by this explicit benchmark runner."""
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    async def save(self, payload, *, key, media_type="application/json"):
        data = canonical_json(payload).encode()
        target = self.root / key
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".tmp")
        with temporary.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(target)
        return ArtifactRef(relative_path=key, sha256=hashlib.sha256(data).hexdigest(), media_type=media_type)

    async def verify(self, ref):
        path = self.root / ref.relative_path
        if path.resolve().is_relative_to(self.root.resolve()) is False or path.is_symlink():
            raise ValueError("unsafe artifact")
        if hashlib.sha256(path.read_bytes()).hexdigest() != ref.sha256:
            raise ValueError("artifact bytes differ")

    async def load(self, ref):
        await self.verify(ref)
        return json.loads((self.root / ref.relative_path).read_bytes())


class HFTokenizer:
    def __init__(self, tokenizer):
        self.tokenizer = tokenizer
    def encode(self, text):
        return self.tokenizer.encode(text, add_special_tokens=False, truncation=False, verbose=False)
    def token_offsets(self, text):
        encoded = self.tokenizer(text, add_special_tokens=False, return_offsets_mapping=True, truncation=False, verbose=False)
        return [tuple(span) for span in encoded["offset_mapping"] if span[1] > span[0]]


async def benchmark(args):
    import psutil
    import torch
    from qdrant_client import QdrantClient
    from transformers import AutoTokenizer
    torch.set_num_threads(args.threads)
    slug = args.model.split("/")[-1]
    workspace = Path(args.work_directory).resolve() / slug
    workspace.mkdir(parents=True, exist_ok=True)
    result_file = ROOT / "docs/v2/suhyeon" / ("benchmark-results-" + slug + ".json")
    fixture = json.loads((ROOT / "tests/fixtures/v2/suhyeon/retrieval_queries.json").read_text())
    original = [*legacy_corpus_documents(ROOT / "docs/data/technology_competition"),
                *legacy_corpus_documents(ROOT / "docs/data/market")]
    # Entire original files count toward the pool; exclude the 37-page OpenVLA
    # paper and all generated notes. The resulting actual pool is 193 pages.
    original = [document for document in original if document.source_id != "OPENVLA_2406_09246_V3"]
    provenance = [{"source_id": document.source_id, "original_company_ids": list(document.company_ids),
                   "original_evidence_scope": document.evidence_scope, "path": str(document.path.relative_to(ROOT))}
                  for document in original]
    evaluation = [replace(document, company_ids=("benchmark_physical_ai",), company_names=("Synthetic benchmark scope",))
                  for document in original]
    tokenizers = {
        spec.model_id: AutoTokenizer.from_pretrained(spec.model_id, revision=spec.revision,
            cache_dir=args.cache, local_files_only=True, trust_remote_code=False, use_fast=True)
        for spec in MODEL_SPECS.values()
    }
    # Prefix and special tokens are also fitted inside the shared 400 budget.
    counters = [lambda text, tokenizer=tokenizers[spec.model_id], prefix=spec.passage_prefix:
                len(tokenizer.encode(prefix + text, add_special_tokens=True, truncation=False, verbose=False))
                for spec in MODEL_SPECS.values()]
    store = BenchmarkArtifacts(workspace / "artifacts")
    corpus = await ingest_documents(evaluation, store=store, corpus_version="benchmark-physical-ai-1.0",
        tokenizer=HFTokenizer(tokenizers["intfloat/multilingual-e5-base"]), count_tokens=counters)
    scope_ref = await store.save({"purpose": "evaluation_only_not_production_company_attribution",
                                 "evaluation_owner": "benchmark_physical_ai", "corpus_hash": corpus.corpus_hash,
                                 "original_source_provenance": provenance}, key="benchmark/original-ownership-manifest.json")
    assert corpus.total_pages <= 200
    assert all(counter(chunk.text) <= 400 for counter in counters for chunk in corpus.chunks)
    process = psutil.Process()
    peak = [process.memory_info().rss]
    done = threading.Event()
    def sample():
        while not done.wait(0.05):
            peak[0] = max(peak[0], process.memory_info().rss)
    thread = threading.Thread(target=sample, daemon=True)
    thread.start()
    backend = SentenceTransformerEmbeddings(args.model, cache_folder=args.cache, device=args.device,
                                           batch_size=args.batch_size, local_files_only=True)
    client = QdrantClient(path=str(workspace / "qdrant"))
    retriever = NeutralRetriever(corpus=corpus, embedding=backend, store=store, client=client)
    result = {
        "schema_version": "1.0", "measurement_status": "started",
        "label_provenance": fixture["label_provenance"], "human_label_reviewed": False,
        "production_company_attribution_valid": False,
        "benchmark_scope": "Synthetic domain-context owner; original company ownership preserved below",
        "original_source_provenance": provenance, "embedding": backend.metadata,
        "original_ownership_manifest_ref": scope_ref.model_dump(mode="json"),
        "corpus_hash": corpus.corpus_hash, "corpus_total_pages": corpus.total_pages,
        "chunk_count": len(corpus.chunks), "chunk_manifest_hash": retriever.manifest["chunk_manifest_hash"],
        "index_hash": retriever.index_hash, "index_revision": retriever.index_revision,
        "collection_name": retriever.collection_name,
        "query_fixture_sha256": hashlib.sha256((ROOT / "tests/fixtures/v2/suhyeon/retrieval_queries.json").read_bytes()).hexdigest(),
        "runtime": {"python": platform.python_version(), "platform": platform.platform(), "machine": platform.machine(),
                    "torch": torch.__version__, "sentence_transformers": importlib.metadata.version("sentence-transformers"),
                    "qdrant_client": importlib.metadata.version("qdrant-client"), "threads": args.threads,
                    "batch_size": args.batch_size, "device": args.device},
        "queries": [],
    }
    result_file.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    try:
        start = time.perf_counter()
        await retriever.initialize()
        result["indexing_seconds_including_model_load"] = time.perf_counter() - start
        print(args.model, "index built", corpus.total_pages, "pages", len(corpus.chunks), "chunks", flush=True)
        await retriever.retrieve("robot manipulation technology", company_id="benchmark_physical_ai",
                                 domain=Domain.PHYSICAL_AI_ROBOTICS, as_of_date=date(2026, 10, 13))
        for query in fixture["queries"]:
            start = time.perf_counter()
            retrieval = await retriever.retrieve(query["query"], company_id=query["company_id"],
                domain=Domain(query["domain"]), as_of_date=date.fromisoformat(query["as_of_date"]))
            elapsed = time.perf_counter() - start
            result["queries"].append({"query_id": query["query_id"], "perspective": query["perspective"],
                "language": query["language"], "query": query["query"], "relevant_source_ids": query["relevant_source_ids"],
                "ranked_hits": [hit.model_dump() for hit in retrieval.receipt.ranked_hits],
                "receipt": retrieval.receipt.model_dump(mode="json"), "latency_seconds": elapsed})
            print(query["query_id"], retrieval.receipt.outcome, round(elapsed, 3), flush=True)
        result["metrics"] = ranking_metrics(result["queries"])
        latencies = sorted(q["latency_seconds"] for q in result["queries"])
        result["query_latency_p50_seconds"] = statistics.median(latencies)
        # Linear-interpolated percentile, explicit convention for n=20.
        position = 0.95 * (len(latencies) - 1)
        lo = int(position)
        result["query_latency_p95_seconds"] = latencies[lo] + (position - lo) * (latencies[min(lo + 1, len(latencies) - 1)] - latencies[lo])
        result["measurement_status"] = "completed_provisional_labels"
    except Exception as exc:
        result["measurement_status"] = "failed"
        result["error"] = {"type": type(exc).__name__, "message": str(exc)}
        raise
    finally:
        done.set()
        thread.join(timeout=1)
        result["sampled_peak_process_rss_bytes"] = max(peak[0], process.memory_info().rss)
        result["memory_method"] = "50ms sampled process RSS, includes model loading/index/query; excludes tokenizer/corpus preflight peak"
        client.close()
        result_file.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result["metrics"], indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", choices=list(MODEL_SPECS), required=True)
    parser.add_argument("--cache", required=True)
    parser.add_argument("--work-directory", required=True)
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--batch-size", type=int, default=8)
    asyncio.run(benchmark(parser.parse_args()))
