"""Small persistent vector index with company filters and page citations.

Retrieval scores rank documents only; they never become evaluation scores.
The index is local. Embeddings use the existing OpenAI embedding backend.
"""
from __future__ import annotations

import hashlib
import json
import math
from functools import lru_cache
from pathlib import Path
from threading import RLock
from uuid import uuid4

from main.paths import PROJECT_ROOT
from main.rag.embeddings import OpenAIEmbeddings, validate_vectors

CORPUS = PROJECT_ROOT / "docs/data/technology_competition"
INDEX = PROJECT_ROOT / "outputs/rag/company_pdf_index.json"
QUERIES = {
    "technology": {
        "problem_solution": "고객 제조 문제 해결 제품 작업 적용 사례 customer manufacturing problem product task deployment outcome",
        "ai_role": "AI 모델 구조 학습 데이터 추론 역할 architecture training data imitation learning vision language action inference",
        "performance_validation": "성능 검증 성공률 시험 조건 표본 수 실험 결과 evaluation task success rate trials baseline Table real world",
        "maturity": "개발 단계 시연 현장 실증 제품 배포 한계 demonstration pilot deployment commercial limitations",
    },
    "competition": {
        "differentiation": "제품 기술 차별성 대상 고객 작업 워크플로우 product differentiation manufacturing dexterous manipulation architecture",
        "comparable_performance": "경쟁 모델 동일 조건 성능 비교 success rate baseline benchmark evaluation protocol robot trials",
        "defensibility": "독자 기술 데이터 권리 학습 노하우 특허 proprietary architecture training data collection patents know how",
        "adoption_risk": "도입 위험 제한 사항 실패 추론 비용 안전성 generalization failure limitations safety deployment cost latency",
    },
}


def _unit(vector):
    norm = math.hypot(*vector)
    return [value / norm for value in vector]


class CompanyPDFRAG:
    def __init__(self, corpus=CORPUS, index_path=INDEX, *, embeddings=None):
        self.corpus, self.index_path = Path(corpus), Path(index_path)
        self.embeddings = embeddings or OpenAIEmbeddings("text-embedding-3-small", 512)
        self._lock = RLock()
        self._vectors = None
        self._query_cache = {}
        self.sources = {
            row["document_id"]: row
            for row in json.loads((self.corpus / "sources.json").read_text())
        }
        self.chunks = self._read_rows("chunks.jsonl")
        self.pages = {
            (row["document_id"], row["page"]): row
            for row in self._read_rows("pages.jsonl")
        }
        ids = [row["chunk_id"] for row in self.chunks]
        if len(ids) != len(set(ids)):
            raise ValueError("PDF corpus contains duplicate chunk IDs")
        for row in self.chunks:
            source = self.sources[row["document_id"]]
            page = self.pages[row["document_id"], row["page"]]
            if page["text"][row["char_start"]:row["char_end"]] != row["text"]:
                raise ValueError("PDF chunk does not match its page text")
            if row["company_ids"] != source["company_ids"]:
                raise ValueError("PDF company metadata disagrees with source manifest")
        digest = hashlib.sha256()
        for name in ("sources.json", "processed/chunks.jsonl", "processed/pages.jsonl"):
            digest.update((self.corpus / name).read_bytes())
        digest.update(f"{self.embeddings.model_name}:{self.embeddings.dimensions}".encode())
        self.fingerprint = digest.hexdigest()

    def _read_rows(self, filename):
        with (self.corpus / "processed" / filename).open(encoding="utf-8") as stream:
            return [json.loads(line) for line in stream if line.strip()]

    def build_index(self, *, rebuild=False):
        with self._lock:
            if self._vectors is not None and not rebuild:
                return
            cached = None
            if self.index_path.exists() and not rebuild:
                cached = json.loads(self.index_path.read_text())
            if cached and cached.get("fingerprint") == self.fingerprint:
                vectors = validate_vectors(cached["vectors"], len(self.chunks), self.embeddings.dimensions)
            else:
                vectors = validate_vectors(
                    self.embeddings.embed_documents([row["text"] for row in self.chunks]),
                    len(self.chunks), self.embeddings.dimensions,
                )
                self.index_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.index_path.with_name(f"{self.index_path.name}.{uuid4().hex}.tmp")
                try:
                    temporary.write_text(json.dumps({
                        "fingerprint": self.fingerprint,
                        "model": self.embeddings.model_name,
                        "dimensions": self.embeddings.dimensions,
                        "vectors": vectors,
                    }))
                    temporary.replace(self.index_path)
                finally:
                    temporary.unlink(missing_ok=True)
            self._vectors = [_unit(vector) for vector in vectors]

    def retrieve(self, company_id: str, company_name: str, role: str, *, pages_per_criterion=2):
        if role not in QUERIES:
            raise ValueError(f"Unknown PDF retrieval role: {role}")
        if type(pages_per_criterion) is not int or not 1 <= pages_per_criterion <= 3:
            raise ValueError("pages_per_criterion must be between 1 and 3")
        candidates = []
        for index, row in enumerate(self.chunks):
            source = self.sources[row["document_id"]]
            if str(company_id) not in {str(value) for value in row["company_ids"]}:
                continue
            if source["evidence_scope"] not in {"company_technical", "company_description"}:
                continue
            # Custom CSVs cannot accidentally attach an old ID to a different company.
            names = source.get("company_names", {})
            aliases = names.get(str(company_id), [])
            if company_name.casefold() not in {name.casefold() for name in aliases}:
                raise ValueError(f"PDF corpus has no matching company name for ID {company_id}")
            candidates.append(index)
        trace = {"role": role, "company_id": str(company_id), "company": company_name,
                 "index_fingerprint": self.fingerprint, "retriever": "openai_embeddings_local_cosine",
                 "queries": [], "retrieved_evidence_ids": [], "company_pdf_available": bool(candidates)}
        if not candidates:
            return [], trace
        page_count = len({(self.chunks[i]["document_id"], self.chunks[i]["page"]) for i in candidates})
        page_limit = page_count if page_count <= 4 else pages_per_criterion
        trace["coverage_policy"] = "all_short_company_documents" if page_count <= 4 else "top_pages_per_criterion"
        with self._lock:
            self.build_index()
            queries = list(QUERIES[role].values())
            missing = [query for query in queries if query not in self._query_cache]
            if missing:
                vectors = validate_vectors(self.embeddings.embed_documents(missing), len(missing), self.embeddings.dimensions)
                self._query_cache.update(zip(missing, map(_unit, vectors)))
            chosen = {}
            for criterion, query in QUERIES[role].items():
                query_vector = self._query_cache[query]
                ranked = sorted(candidates, key=lambda i: -sum(a*b for a,b in zip(query_vector, self._vectors[i])))
                seen_pages = set()
                selected_ids = []
                for index in ranked:
                    row = self.chunks[index]
                    key = row["document_id"], row["page"]
                    if key in seen_pages:
                        continue
                    seen_pages.add(key)
                    evidence_id = f"PDF-{company_id}-{key[0]}-p{key[1]}"
                    selected_ids.append(evidence_id)
                    if key not in chosen:
                        source = self.sources[key[0]]
                        page = self.pages[key]
                        # A whole page keeps nearby trial counts and table headings together.
                        text = row["text"] if row.get("is_page_excerpt") else page["text"]
                        if len(text) > 7000:
                            start = max(0, row["char_start"] - 2200)
                            text = text[start:start+7000]
                        chosen[key] = {
                            "id": evidence_id, "company": company_name,
                            "title": source["title"],
                            "locator": f'{page["source_url"]}#page={key[1]}',
                            "text": text, "date": source["published_date"],
                            "stage": source.get("stage", "unknown"), "is_mock": False,
                            "document_id": key[0], "page": key[1],
                            "source_url": page["source_url"], "file_name": source["file_name"],
                            "source_type": source["source_type"], "evidence_scope": source["evidence_scope"],
                            "is_derived": source["is_derived"], "limitations": source["limitations"],
                        }
                    if len(seen_pages) >= page_limit:
                        break
                trace["queries"].append({"criterion": criterion, "query": query, "evidence_ids": selected_ids})
        evidence = list(chosen.values())
        trace["retrieved_evidence_ids"] = [item["id"] for item in evidence]
        return evidence, trace


@lru_cache(maxsize=1)
def default_pdf_rag():
    return CompanyPDFRAG()
