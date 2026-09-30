"""PDF isolation, persistent cache, and evidence-based score safeguards."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from main.agents.common.evidence import Evidence
from main.agents.common.rag_judgment import RagJudgmentScore, validate_rag_judgment, merge_rag_judgment
from main.agents.common.csv_judgment import COMPETITION_CRITERIA
from main.rag.company_pdf import CompanyPDFRAG


class FakeEmbeddings:
    model_name = "test"
    dimensions = 2

    def __init__(self):
        self.calls = []

    def embed_documents(self, texts):
        self.calls.append(texts)
        return [[1.0, 0.5] for _ in texts]


def corpus(root):
    (root / "processed").mkdir()
    sources, chunks, pages = [], [], []
    for doc, ids, name in [("A", [17], "회사A"), ("B", [14], "회사B"), ("BG", [], "")]:
        scope = "company_technical" if ids else "technical_background"
        source = dict(document_id=doc, company_ids=ids, company_names={str(ids[0]): [name]} if ids else {},
                      evidence_scope=scope, title=doc, source_type="research_paper", is_derived=False,
                      published_date="2026", file_name=f"{doc}.pdf", limitations="자체 실험")
        sources.append(source)
        page = dict(source, page=1, text=f"{doc} 기술 평가 근거", source_url="https://example.org/" + doc)
        pages.append(page)
        chunks.append(dict(page, chunk_id=doc+":p1:c0", char_start=0, char_end=len(page["text"])))
    (root / "sources.json").write_text(json.dumps(sources))
    for name, rows in [("chunks", chunks), ("pages", pages)]:
        (root / "processed" / f"{name}.jsonl").write_text("\n".join(map(json.dumps, rows)))


class PDFRetrievalTests(unittest.TestCase):
    def test_filtering_cache_and_name_identity(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            corpus(root)
            embedder = FakeEmbeddings()
            rag = CompanyPDFRAG(root, root/"index.json", embeddings=embedder)
            hits, trace = rag.retrieve("17", "회사A", "technology")
            self.assertEqual([row["document_id"] for row in hits], ["A"])
            self.assertEqual(len(trace["queries"]), 4)
            self.assertEqual(hits[0]["page"], 1)
            self.assertTrue(all(row["company"] == "회사A" for row in hits))
            with self.assertRaisesRegex(ValueError, "matching company name"):
                rag.retrieve("17", "다른 회사", "competition")
            fresh = FakeEmbeddings()
            reused = CompanyPDFRAG(root, root/"index.json", embeddings=fresh)
            reused.build_index()
            self.assertEqual(fresh.calls, [])
            # A corpus change must invalidate embeddings rather than reuse stale vectors.
            sources = json.loads((root/"sources.json").read_text())
            sources[0]["limitations"] = "갱신된 출처"
            (root/"sources.json").write_text(json.dumps(sources))
            changed = CompanyPDFRAG(root, root/"index.json", embeddings=fresh)
            changed.build_index()
            self.assertEqual(len(fresh.calls), 1)

    def test_no_company_pdf_does_not_call_embedding_api(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            corpus(root)
            embedder = FakeEmbeddings()
            rag = CompanyPDFRAG(root, root/"index.json", embeddings=embedder)
            hits, trace = rag.retrieve("99", "자료 없는 회사", "competition")
            self.assertEqual(hits, [])
            self.assertFalse(trace["company_pdf_available"])
            self.assertEqual(embedder.calls, [])


def evidence(id, company, scope="company_technical"):
    return Evidence(id=id, company=company, document_id=id, title=id, locator="test.pdf#1",
                    page=1, text="시험 자료", evidence_scope=scope)


class PDFScoreTests(unittest.TestCase):
    def setUp(self):
        self.records = [evidence("PDF-A", "대상"), evidence("PDF-B", "경쟁사")]
        self.strict = {"criteria": [dict(criterion=name, rating=0, points=0, rationale="미확인", evidence_ids=[])
                                    for name in COMPETITION_CRITERIA],
                       "total": 0, "max": 100, "status": "insufficient_evidence"}
        self.scores = [RagJudgmentScore(criterion=name, rating=rating, rationale="문서의 구체적 기술 차이",
                                       limitations="동일 조건 검증 없음", evidence_ids=ids)
                       for name, rating, ids in zip(COMPETITION_CRITERIA, (3, 4, 2, 2),
                                                  (["PDF-A", "PDF-B"], ["PDF-A", "PDF-B"], ["PDF-A"], ["PDF-A"]))]

    def test_missing_comparable_test_cannot_be_bypassed(self):
        rows = validate_rag_judgment(self.scores, self.records, "대상", COMPETITION_CRITERIA, self.strict)
        self.assertEqual(rows[1]["rating"], 0)
        result = merge_rag_judgment(self.strict, self.strict, rows)
        self.assertEqual(result["total"], 35)
        self.assertEqual(result["verified_score"]["total"], 0)
        self.assertEqual(result["status"], "provisional")

    def test_wrong_owner_unknown_id_and_background_cannot_support_score(self):
        self.scores[2].evidence_ids = ["PDF-B"]
        with self.assertRaises(ValueError):
            validate_rag_judgment(self.scores, self.records, "대상", COMPETITION_CRITERIA, self.strict)
        self.scores[2].evidence_ids = ["invented"]
        with self.assertRaises(ValueError):
            validate_rag_judgment(self.scores, self.records, "대상", COMPETITION_CRITERIA, self.strict)
        self.scores[2].evidence_ids = ["PDF-A"]
        self.records[0].evidence_scope = "technical_background"
        with self.assertRaises(ValueError):
            validate_rag_judgment(self.scores, self.records, "대상", COMPETITION_CRITERIA, self.strict)

    def test_documented_zero_overrides_higher_csv_fallback(self):
        rows = validate_rag_judgment(self.scores, self.records, "대상", COMPETITION_CRITERIA, self.strict)
        rows[0].update(rating=0, points=0)
        csv = json.loads(json.dumps(self.strict))
        csv["criteria"][0].update(rating=3, points=15, basis="llm_csv_assessment")
        result = merge_rag_judgment(self.strict, csv, rows)
        self.assertEqual(result["criteria"][0]["rating"], 0)


if __name__ == "__main__":
    unittest.main()
