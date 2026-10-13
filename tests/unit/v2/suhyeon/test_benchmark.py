from main.rag.neutral.retrieval import ranking_metrics
import hashlib
import json
from collections import Counter
from pathlib import Path
from pypdf import PdfReader


def test_ranking_metrics_use_first_relevant_actual_rank_and_zero_for_miss():
    rows = [
        {"perspective": "market", "relevant_source_ids": ["good"], "ranked_hits": [
            {"source_id": "bad", "rank": 1}, {"source_id": "good", "rank": 2}, {"source_id": "good", "rank": 4}]},
        {"perspective": "market", "relevant_source_ids": ["good"], "ranked_hits": [{"source_id": "bad", "rank": 1}]},
        {"perspective": "technology_maturity", "relevant_source_ids": ["good"], "ranked_hits": [{"source_id": "good", "rank": 1}]},
    ]
    metrics = ranking_metrics(rows)
    assert metrics["market"] == {"query_count": 2, "hit_rate_at_5": 0.5, "mrr_at_5": 0.25}
    assert metrics["overall"] == {"query_count": 3, "hit_rate_at_5": 2 / 3, "mrr_at_5": 0.5}


def test_benchmark_fixture_balance_and_exact_original_label_quotes():
    root = Path(__file__).resolve().parents[4]
    fixture = json.loads((root / "tests/fixtures/v2/suhyeon/retrieval_queries.json").read_text())
    assert fixture["human_label_reviewed"] is False
    queries = fixture["queries"]
    assert len({q["query_id"] for q in queries}) == 20
    assert Counter(q["language"] for q in queries) == {"ko": 10, "en": 10}
    assert Counter(q["perspective"] for q in queries) == {
        "technology_maturity": 5, "market": 5, "stakeholders": 5, "domain_application": 5,
    }
    for query in queries:
        assert query["relevant_source_ids"]
        for label in query["label_evidence"]:
            text = PdfReader(root / label["pdf_file"]).pages[label["page_number"] - 1].extract_text()
            assert label["quote"] in text
            assert hashlib.sha256(label["quote"].encode()).hexdigest() == label["quote_sha256"]
