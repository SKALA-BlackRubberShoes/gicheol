import asyncio
import base64
import hashlib
import json
import logging
from dataclasses import replace
from datetime import date, datetime, timezone

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from main.agents.supervisor.contracts import ArtifactRef, Domain, canonical_json
from main.rag.neutral.ingestion import (
    CorpusDomainUnavailable, CorpusIngestionError, DocumentInput,
    document_inputs_from_json, ingest_documents, legacy_corpus_documents,
)


class MemoryStore:
    def __init__(self):
        self.payloads = {}
        self.saved = []

    async def save(self, payload, *, key, media_type="application/json"):
        raw = canonical_json(payload).encode()
        ref = ArtifactRef(relative_path=key, sha256=hashlib.sha256(raw).hexdigest(), media_type=media_type)
        self.payloads[key] = json.loads(raw)
        self.saved.append(ref)
        return ref

    async def verify(self, ref):
        assert hashlib.sha256(canonical_json(self.payloads[ref.relative_path]).encode()).hexdigest() == ref.sha256

    async def load(self, ref):
        await self.verify(ref)
        return self.payloads[ref.relative_path]


def pdf(path, pages=1, *, text="Company research quote", encrypt=False):
    writer = PdfWriter()
    font = writer._add_object(DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                                               NameObject("/Subtype"): NameObject("/Type1"),
                                               NameObject("/BaseFont"): NameObject("/Helvetica")}))
    for number in range(pages):
        page = writer.add_blank_page(width=612, height=792)
        if text:
            stream = DecodedStreamObject()
            stream.set_data(f"BT /F1 12 Tf 72 700 Td ({text} page {number + 1}) Tj ET".encode("ascii"))
            page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
            page[NameObject("/Contents")] = writer._add_object(stream)
    if encrypt:
        writer.encrypt("password")
    writer.write(path)
    return path


def document(path, *, source="source"):
    return DocumentInput(path=path, source_id=source, company_ids=("14",),
                         company_names=("RLWRLD",), domain=Domain.PHYSICAL_AI_ROBOTICS,
                         publisher_group="company_14", title="Technical report", publisher="RLWRLD")


def ingest(documents, store, **kwargs):
    return asyncio.run(ingest_documents(documents, store=store, **kwargs))


def test_actual_pdf_bytes_pages_exact_quotes_and_separate_hashes_preserved(tmp_path):
    path = pdf(tmp_path / "source.pdf", pages=2)
    store = MemoryStore()
    corpus = ingest([document(path)], store, company_aliases={"14": ["리얼월드", "RLWRLD"]})
    assert corpus.total_pages == 2
    assert {page.page_number for page in corpus.pages} == {1, 2}
    source = corpus.sources["source"]
    payload = store.payloads[source.content_ref.relative_path]
    assert source.sha256 == source.content_ref.sha256
    assert source.sha256 != hashlib.sha256(path.read_bytes()).hexdigest()
    assert payload["original_pdf_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert base64.b64decode(payload["original_pdf_base64"], validate=True) == path.read_bytes()
    assert payload["body_verified"] is True
    assert payload["published_at"] is None
    assert [entry["page_number"] for entry in payload["pages"]] == [1, 2]
    for chunk in corpus.chunks:
        assert payload["pages"][chunk.page_number - 1]["text"][chunk.char_start:chunk.char_end] == chunk.text
    corpus.ensure_available("14", Domain.PHYSICAL_AI_ROBOTICS)
    with pytest.raises(CorpusDomainUnavailable):
        corpus.ensure_available("14", Domain.ENERGY)
    with pytest.raises(CorpusDomainUnavailable):
        corpus.ensure_available("17", Domain.PHYSICAL_AI_ROBOTICS)


def test_real_201_page_pdf_fails_before_any_artifact_or_index_write(tmp_path):
    path = pdf(tmp_path / "large.pdf", pages=201, text="")
    store = MemoryStore()
    with pytest.raises(CorpusIngestionError, match="200-page"):
        ingest([document(path)], store)
    assert store.saved == []


def test_actual_page_budget_applies_to_full_pool_and_200_is_allowed(tmp_path):
    large = pdf(tmp_path / "large.pdf", pages=200, text="")
    extra = pdf(tmp_path / "extra.pdf", text="different")
    store = MemoryStore()
    with pytest.raises(CorpusIngestionError, match="200-page"):
        ingest([document(large), document(extra, source="extra")], store)
    assert store.saved == []
    assert ingest([document(large)], MemoryStore()).total_pages == 200


def test_duplicate_actual_hash_is_counted_once_and_manifest_records_alias(tmp_path):
    original = pdf(tmp_path / "original.pdf", pages=200, text="")
    copy = tmp_path / "copy.pdf"
    copy.write_bytes(original.read_bytes())
    store = MemoryStore()
    corpus = ingest([document(original), document(copy, source="copy")], store)
    manifest = store.payloads[corpus.manifest_ref.relative_path]
    assert corpus.total_pages == 200
    assert len(corpus.sources) == 1
    assert manifest["duplicates"][0]["duplicate_of"] == "source"
    assert manifest["duplicates"][0]["original_pdf_sha256"] == hashlib.sha256(original.read_bytes()).hexdigest()


@pytest.mark.parametrize("failure", ["encrypted", "corrupt", "missing"])
def test_encryption_parse_failure_or_inaccessible_source_prevents_all_saves(tmp_path, failure):
    good = pdf(tmp_path / "good.pdf")
    bad = tmp_path / "bad.pdf"
    if failure == "encrypted":
        pdf(bad, encrypt=True)
    elif failure == "corrupt":
        bad.write_bytes(b"%PDF-1.7\ncorrupt object body\n%%EOF")
    store = MemoryStore()
    original_handlers = tuple(logging.getLogger("pypdf").handlers)
    with pytest.raises(CorpusIngestionError):
        ingest([document(good), document(bad, source="bad")], store)
    assert store.saved == []
    assert tuple(logging.getLogger("pypdf").handlers) == original_handlers


def test_manifest_claims_cannot_override_real_hash_or_page_count(tmp_path):
    path = pdf(tmp_path / "source.pdf")
    for changed in [replace(document(path), expected_page_count=2), replace(document(path), expected_sha256="f" * 64)]:
        store = MemoryStore()
        with pytest.raises(CorpusIngestionError):
            ingest([changed], store)
        assert store.saved == []


def test_company_alias_mismatch_is_rejected_before_save(tmp_path):
    path = pdf(tmp_path / "source.pdf")
    store = MemoryStore()
    with pytest.raises(CorpusIngestionError, match="alias mismatch"):
        ingest([document(path)], store, company_aliases={"14": ["CarbonSix"]})
    assert store.saved == []


def test_page_excerpt_excludes_other_companies_but_counts_all_pages(tmp_path):
    path = pdf(tmp_path / "source.pdf", pages=2, text="OtherStart TargetCompany own-result OtherEnd unrelated")
    scoped = replace(document(path), evidence_page_numbers=(2,),
                     page_excerpts={2: ("TargetCompany", "OtherEnd")})
    corpus = ingest([scoped], MemoryStore())
    assert corpus.total_pages == 2
    assert all(chunk.page_number == 2 for chunk in corpus.chunks)
    assert all("OtherStart" not in chunk.text and "OtherEnd" not in chunk.text for chunk in corpus.chunks)
    assert all(corpus.pages[chunk.page_number - 1].text[chunk.char_start:chunk.char_end] == chunk.text for chunk in corpus.chunks)


def test_invalid_excerpt_or_page_fails_before_save(tmp_path):
    path = pdf(tmp_path / "source.pdf")
    for scoped in [replace(document(path), evidence_page_numbers=(2,)),
                   replace(document(path), page_excerpts={1: ("not present", "missing")})]:
        store = MemoryStore()
        with pytest.raises(CorpusIngestionError):
            ingest([scoped], store)
        assert store.saved == []


def test_unknown_or_partial_publication_metadata_stays_null(tmp_path):
    path = pdf(tmp_path / "source.pdf")
    metadata = {"path": path.name, "source_id": "source", "company_ids": ["14"],
                "company_names": ["RLWRLD"], "domain": Domain.PHYSICAL_AI_ROBOTICS.value,
                "publisher_group": "company_14", "title": "report"}
    input_path = tmp_path / "input.json"
    for published in [None, "unknown", "2026", "2026-06", "2026-05-06"]:
        input_path.write_text(json.dumps({"documents": [{**metadata, "published_at": published}]}))
        loaded = document_inputs_from_json(input_path)
        assert loaded[0].published_at == (date(2026, 5, 6) if published == "2026-05-06" else None)


def test_legacy_builder_keeps_original_sources_company_restrictions_and_dates():
    from pathlib import Path

    root = Path(__file__).resolve().parents[4]
    documents = legacy_corpus_documents(root / "docs/data/technology_competition")
    assert len(documents) == 5
    assert all(not doc.is_derived for doc in documents)
    startup = next(doc for doc in documents if doc.source_id == "STARTUPRECIPE_20260608")
    assert startup.company_ids == ("17",)
    assert startup.company_names == ("카본식스",)
    assert startup.evidence_page_numbers == (4,)
    assert startup.page_excerpts[4] == ("카본식스", "더플레이토")
    assert startup.published_at is None
    assert startup.publication_label == "2026-06"
    assert any(doc.company_ids == () for doc in documents)


def test_explicitly_included_derived_summary_cannot_claim_original_web_origin():
    from pathlib import Path

    root = Path(__file__).resolve().parents[4]
    documents = legacy_corpus_documents(root / "docs/data/technology_competition", include_derived=True)
    derived = next(doc for doc in documents if doc.is_derived)
    assert derived.original_url is None
    assert "원문" in derived.limitations


def test_known_recoverable_original_pdf_parser_warning_is_recorded_in_provenance():
    from pathlib import Path

    root = Path(__file__).resolve().parents[4]
    documents = legacy_corpus_documents(root / "docs/data/technology_competition")
    diffusion = next(doc for doc in documents if doc.source_id == "DIFFUSION_POLICY_IJRR_2024")
    store = MemoryStore()
    corpus = ingest([diffusion], store)
    payload = store.payloads[corpus.sources[diffusion.source_id].content_ref.relative_path]
    assert any("invalid; use 0.0 instead" in message for message in payload["parse_diagnostics"])
    manifest = store.payloads[corpus.manifest_ref.relative_path]
    assert manifest["documents"][0]["parse_diagnostics"] == payload["parse_diagnostics"]
    assert corpus.total_pages == 19


def test_reingestion_does_not_overwrite_older_source_or_manifest_snapshot(tmp_path):
    path = pdf(tmp_path / "source.pdf")
    store = MemoryStore()
    first_document = replace(document(path), retrieved_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
    later_document = replace(first_document, retrieved_at=datetime(2026, 10, 2, tzinfo=timezone.utc))
    first = ingest([first_document], store)
    later = ingest([later_document], store)
    assert first.corpus_hash == later.corpus_hash
    assert first.source_refs["source"].relative_path != later.source_refs["source"].relative_path
    assert first.manifest_ref.relative_path != later.manifest_ref.relative_path
    asyncio.run(store.verify(first.source_refs["source"]))
    asyncio.run(store.verify(first.manifest_ref))
