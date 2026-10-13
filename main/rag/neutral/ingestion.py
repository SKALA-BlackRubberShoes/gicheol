"""Validate the whole real PDF pool before creating any RAG artifacts.

ArtifactStore.save accepts JSON objects.  Consequently the source artifact is
a JSON envelope containing lossless base64 PDF bytes, their original SHA-256,
and exact extracted page text.  Source.sha256 is the *envelope artifact* hash,
as required by the shared Source contract; it is never mislabeled as PDF hash.
"""
from __future__ import annotations

import asyncio
import base64
import csv
import hashlib
import io
import json
import logging
import re
import unicodedata
from threading import get_ident
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Literal, Mapping, Sequence

from pypdf import PdfReader

from main.agents.supervisor.contracts import (
    ArtifactRef, ArtifactStore, Domain, Source, TerminalReason, canonical_hash,
)
from main.rag.neutral.chunking import Chunk, DocumentPage, Tokenizer, chunk_pages


class CorpusIngestionError(ValueError):
    code = TerminalReason.CONFIG_OR_AUTH_ERROR


class CorpusDomainUnavailable(CorpusIngestionError):
    code = TerminalReason.CORPUS_DOMAIN_UNAVAILABLE


def _name(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


@dataclass(frozen=True)
class DocumentInput:
    path: Path | str
    source_id: str
    company_ids: tuple[str, ...]
    company_names: tuple[str, ...]
    domain: Domain
    publisher_group: str
    title: str
    publisher: str | None = None
    source_type: Literal["company", "paper", "institution", "news", "dataset"] = "company"
    corporate_controlled: bool = True
    document_id: str | None = None
    original_url: str | None = None
    published_at: date | None = None
    language: str | None = None
    retrieved_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expected_sha256: str | None = None
    expected_page_count: int | None = None
    evidence_page_numbers: tuple[int, ...] | None = None
    page_excerpts: Mapping[int, tuple[str, str]] = field(default_factory=dict)
    is_derived: bool = False
    limitations: str | None = None
    evidence_scope: str | None = None
    publication_label: str | None = None
    retrieved_label: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))
        object.__setattr__(self, "domain", Domain(self.domain))
        object.__setattr__(self, "company_ids", tuple(self.company_ids))
        object.__setattr__(self, "company_names", tuple(self.company_names))
        if len(self.company_ids) != len(self.company_names):
            raise CorpusIngestionError("company IDs and company names must be aligned")
        if len(set(self.company_ids)) != len(self.company_ids):
            raise CorpusIngestionError("document has duplicate company IDs")
        if any(not isinstance(value, str) or not value.strip() for value in (*self.company_ids, *self.company_names)):
            raise CorpusIngestionError("company IDs and names must be nonempty strings")
        if self.retrieved_at.tzinfo is None or self.retrieved_at.utcoffset() is None:
            raise CorpusIngestionError("retrieved_at must include a timezone")
        if self.published_at is not None and type(self.published_at) is not date:
            raise CorpusIngestionError("published_at must be an exact date or None; partial dates stay unknown")
        if self.expected_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", self.expected_sha256):
            raise CorpusIngestionError("invalid expected PDF SHA-256")
        if self.expected_page_count is not None and (type(self.expected_page_count) is not int or self.expected_page_count <= 0):
            raise CorpusIngestionError("expected page count must be a positive integer")
        if self.evidence_page_numbers is not None and any(type(number) is not int or number <= 0 for number in self.evidence_page_numbers):
            raise CorpusIngestionError("evidence page numbers must be positive integers")
        # Validate the shared Source fields before saving anything.  This is a
        # temporary validation DTO, not another common DTO definition.
        _source(self, ArtifactRef(relative_path="validation/source.json", sha256="0" * 64, media_type="application/json"))


def _source(document: DocumentInput, content_ref: ArtifactRef) -> Source:
    return Source(
        source_id=document.source_id, canonical_url=document.original_url,
        original_file_id=document.path.name,
        title=document.title, publisher=document.publisher,
        publisher_group=document.publisher_group, source_type=document.source_type,
        published_at=document.published_at, retrieved_at=document.retrieved_at,
        corporate_controlled=document.corporate_controlled,
        sha256=content_ref.sha256, content_ref=content_ref,
    )


@dataclass(frozen=True)
class Corpus:
    sources: dict[str, Source]
    source_refs: dict[str, ArtifactRef]
    pages: tuple[DocumentPage, ...]
    chunks: tuple[Chunk, ...]
    corpus_hash: str
    corpus_version: str
    manifest_ref: ArtifactRef
    total_pages: int

    def ensure_available(self, company_id: str, domain: Domain) -> None:
        if not any(company_id in chunk.company_ids and chunk.domain == domain for chunk in self.chunks):
            raise CorpusDomainUnavailable(f"no verified PDF corpus for company {company_id!r} in domain {Domain(domain).value!r}")


@dataclass(frozen=True)
class _PreparedDocument:
    document: DocumentInput
    original_bytes: bytes
    original_sha256: str
    texts: tuple[str, ...]
    parse_diagnostics: tuple[str, ...]


class _ParseDiagnostics(logging.Handler):
    """Capture this parser thread's warnings without changing logger config.

    logging.addHandler/removeHandler hold the logging lock.  Concurrent PDF
    ingestions get their own handler and thread filter, so warnings from a
    different run are never written into this document's provenance.
    """

    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.worker_thread = get_ident()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.thread == self.worker_thread:
            self.messages.append(record.getMessage())


def _prepare(
    documents: Sequence[DocumentInput],
    company_aliases: Mapping[str, Sequence[str]] | None,
) -> tuple[list[_PreparedDocument], list[dict[str, object]]]:
    if not documents:
        raise CorpusIngestionError("PDF corpus must contain at least one document")
    if len({doc.source_id for doc in documents}) != len(documents):
        raise CorpusIngestionError("PDF corpus has duplicate source IDs")
    if len({doc.document_id or doc.source_id for doc in documents}) != len(documents):
        raise CorpusIngestionError("PDF corpus has duplicate document IDs")
    prepared: list[_PreparedDocument] = []
    by_hash: dict[str, int] = {}
    duplicates: list[dict[str, object]] = []
    total_pages = 0
    for doc in documents:
        if company_aliases is not None:
            for company_id, company_name in zip(doc.company_ids, doc.company_names, strict=True):
                if company_id not in company_aliases or _name(company_name) not in {_name(alias) for alias in company_aliases[company_id]}:
                    raise CorpusIngestionError(f"company ID/name alias mismatch for {company_id!r}")
        try:
            raw = doc.path.read_bytes()
        except OSError as exc:
            raise CorpusIngestionError(f"PDF source is inaccessible: {doc.path.name}") from exc
        digest = hashlib.sha256(raw).hexdigest()
        if doc.expected_sha256 is not None and digest != doc.expected_sha256:
            raise CorpusIngestionError("PDF bytes differ from declared manifest SHA-256")
        if digest in by_hash:
            first_index = by_hash[digest]
            first = prepared[first_index]
            if doc.expected_page_count is not None and doc.expected_page_count != len(first.texts):
                raise CorpusIngestionError("duplicate PDF has incorrect declared page count")
            if (doc.domain, doc.publisher_group, doc.published_at, doc.source_type, doc.corporate_controlled, doc.evidence_page_numbers, doc.page_excerpts) != (
                first.document.domain, first.document.publisher_group, first.document.published_at,
                first.document.source_type, first.document.corporate_controlled, first.document.evidence_page_numbers, first.document.page_excerpts,
            ):
                raise CorpusIngestionError("identical PDF has conflicting attribution metadata")
            # A multi-company document is parsed once and filtered for each
            # declared identity.  Duplicate files cannot count as new sources.
            company_names = dict(zip(first.document.company_ids, first.document.company_names, strict=True))
            for company_id, company_name in zip(doc.company_ids, doc.company_names, strict=True):
                company_names.setdefault(company_id, company_name)
            merged = replace(first.document, company_ids=tuple(company_names), company_names=tuple(company_names.values()))
            prepared[first_index] = replace(first, document=merged)
            duplicates.append({"document_id": doc.document_id or doc.source_id, "source_id": doc.source_id,
                               "original_pdf_sha256": digest, "duplicate_of": first.document.document_id or first.document.source_id})
            continue
        diagnostics = _ParseDiagnostics()
        parser_logger = logging.getLogger("pypdf")
        parser_logger.addHandler(diagnostics)
        try:
            if not raw.startswith(b"%PDF-") or b"%%EOF" not in raw[-4096:]:
                raise ValueError("invalid PDF header or end marker")
            reader = PdfReader(io.BytesIO(raw), strict=True)
            if reader.is_encrypted:
                raise CorpusIngestionError("encrypted PDFs are rejected before indexing")
            page_count = len(reader.pages)
            if not page_count:
                raise ValueError("PDF contains no pages")
            if doc.expected_page_count is not None and page_count != doc.expected_page_count:
                raise CorpusIngestionError("actual PDF page count differs from the declared manifest")
            total_pages += page_count
            if total_pages > 200:
                raise CorpusIngestionError("actual PDF corpus exceeds the 200-page limit")
            texts = tuple(page.extract_text() or "" for page in reader.pages)
            if doc.evidence_page_numbers is not None and any(number > page_count for number in doc.evidence_page_numbers):
                raise CorpusIngestionError("company evidence page is outside the actual PDF")
            for number, markers in doc.page_excerpts.items():
                if type(number) is not int or not 1 <= number <= page_count or len(markers) != 2:
                    raise CorpusIngestionError("invalid company page excerpt restriction")
                start_marker, end_marker = markers
                text = texts[number - 1]
                start = text.find(start_marker)
                if not start_marker or not end_marker or start < 0 or text.find(end_marker, start + len(start_marker)) < 0:
                    raise CorpusIngestionError("declared company excerpt markers are missing from the actual page")
        except CorpusIngestionError:
            raise
        except Exception as exc:
            raise CorpusIngestionError(f"PDF parsing failed before indexing: {doc.path.name}") from exc
        finally:
            parser_logger.removeHandler(diagnostics)
        by_hash[digest] = len(prepared)
        prepared.append(_PreparedDocument(doc, raw, digest, texts, tuple(diagnostics.messages)))
    return prepared, duplicates


async def ingest_documents(
    documents: Sequence[DocumentInput],
    *,
    store: ArtifactStore,
    corpus_version: str = "1.0",
    company_aliases: Mapping[str, Sequence[str]] | None = None,
    tokenizer: Tokenizer | None = None,
    max_tokens: int = 400,
    overlap_tokens: int = 60,
    count_tokens: Sequence[Callable[[str], int]] = (),
) -> Corpus:
    """Preflight all PDF bytes, counts, names and text, then persist artifacts."""
    if not isinstance(corpus_version, str) or not corpus_version.strip():
        raise CorpusIngestionError("corpus_version must be a nonempty string")
    prepared, duplicates = await asyncio.to_thread(_prepare, tuple(documents), company_aliases)
    pages = tuple(
        DocumentPage(
            document_id=item.document.document_id or item.document.source_id,
            source_id=item.document.source_id, company_ids=item.document.company_ids,
            domain=item.document.domain, publisher_group=item.document.publisher_group,
            page_number=number, text=text, original_sha256=item.original_sha256,
            published_at=item.document.published_at, language=item.document.language,
        )
        for item in prepared for number, text in enumerate(item.texts, 1)
    )
    inputs_by_source = {item.document.source_id: item.document for item in prepared}
    scoped_pages = []
    for page in pages:
        document = inputs_by_source[page.source_id]
        if document.evidence_page_numbers is not None and page.page_number not in document.evidence_page_numbers:
            continue
        markers = document.page_excerpts.get(page.page_number)
        if markers is not None:
            start = page.text.index(markers[0])
            end = page.text.index(markers[1], start + len(markers[0]))
            scoped_pages.append(replace(page, text=page.text[start:end], char_offset=start))
        else:
            scoped_pages.append(page)
    chunks = await asyncio.to_thread(chunk_pages, scoped_pages, tokenizer=tokenizer,
                                    max_tokens=max_tokens, overlap_tokens=overlap_tokens,
                                    count_tokens=count_tokens)
    records = [{
        "document_id": item.document.document_id or item.document.source_id,
        "source_id": item.document.source_id, "original_pdf_sha256": item.original_sha256,
        "page_count": len(item.texts), "company_ids": list(item.document.company_ids),
        "company_names": list(item.document.company_names), "domain": item.document.domain.value,
        "publisher_group": item.document.publisher_group, "title": item.document.title,
        "publisher": item.document.publisher, "source_type": item.document.source_type,
        "corporate_controlled": item.document.corporate_controlled,
        "original_url": item.document.original_url,
        "published_at": item.document.published_at.isoformat() if item.document.published_at else None,
        "language": item.document.language,
        "evidence_page_numbers": list(item.document.evidence_page_numbers) if item.document.evidence_page_numbers is not None else None,
        "page_excerpts": {str(number): list(markers) for number, markers in item.document.page_excerpts.items()},
        "is_derived": item.document.is_derived, "limitations": item.document.limitations,
        "evidence_scope": item.document.evidence_scope,
        "publication_label": item.document.publication_label,
        "retrieved_label": item.document.retrieved_label,
        "parse_diagnostics": list(item.parse_diagnostics),
    } for item in prepared]
    # Corpus identity includes attribution and exact extraction, not an
    # operational retrieved timestamp that changes on the next run.
    corpus_hash = canonical_hash({"corpus_version": corpus_version, "documents": records,
                                  "pages": [{"source_id": page.source_id, "page_number": page.page_number, "text": page.text} for page in pages],
                                  "chunks": [{"chunk_id": chunk.chunk_id, "text": chunk.text} for chunk in chunks],
                                  "chunk_tokens": max_tokens, "chunk_overlap_tokens": overlap_tokens})
    sources: dict[str, Source] = {}
    source_refs: dict[str, ArtifactRef] = {}
    for item, record in zip(prepared, records, strict=True):
        payload = dict(record)
        payload.update({"body_verified": True, "original_pdf_encoding": "base64",
                        "original_pdf_base64": base64.b64encode(item.original_bytes).decode("ascii"),
                        "pages": [{"page_number": number, "text": text} for number, text in enumerate(item.texts, 1)]})
        # Keys use hashes because contract identifiers are not safe filenames.
        source_key = hashlib.sha256(item.document.source_id.encode()).hexdigest()
        content_ref = await store.save(payload, key=f"corpus/{corpus_hash}/content/{source_key}.json")
        await store.verify(content_ref)
        source = _source(item.document, content_ref)
        source_payload = source.model_dump(mode="json")
        source_ref = await store.save(source_payload, key=f"corpus/{corpus_hash}/sources/{source_key}/{canonical_hash(source_payload)}.json")
        await store.verify(source_ref)
        sources[source.source_id] = source
        source_refs[source.source_id] = source_ref
        record["content_ref"] = content_ref.model_dump(mode="json")
        record["source_ref"] = source_ref.model_dump(mode="json")
    manifest = {"schema_version": "1.0", "corpus_version": corpus_version,
                "corpus_hash": corpus_hash, "total_pages": len(pages),
                "documents": records, "duplicates": duplicates,
                "source_refs": {key: ref.model_dump(mode="json") for key, ref in source_refs.items()},
                "chunks": [{"chunk_id": chunk.chunk_id, "source_id": chunk.source_id,
                            "document_id": chunk.document_id, "page_number": chunk.page_number,
                            "char_start": chunk.char_start, "char_end": chunk.char_end,
                            "token_count": chunk.token_count, "text": chunk.text,
                            "company_ids": list(chunk.company_ids), "domain": chunk.domain.value,
                            "publisher_group": chunk.publisher_group, "original_pdf_sha256": chunk.original_sha256,
                            "published_at": chunk.published_at.isoformat() if chunk.published_at else None,
                            "event_date": chunk.event_date.isoformat() if chunk.event_date else None,
                            "language": chunk.language} for chunk in chunks]}
    manifest_ref = await store.save(manifest, key=f"corpus/{corpus_hash}/manifests/{canonical_hash(manifest)}.json")
    await store.verify(manifest_ref)
    return Corpus(sources=sources, source_refs=source_refs, pages=pages, chunks=chunks,
                  corpus_hash=corpus_hash, corpus_version=corpus_version,
                  manifest_ref=manifest_ref, total_pages=len(pages))


def document_inputs_from_json(path: Path | str, *, base_directory: Path | str | None = None) -> tuple[DocumentInput, ...]:
    """Read explicit ingestion metadata from input.json without invented dates.

    The object must have a ``documents`` (or ``corpus_manifest``) list using
    DocumentInput field names.  A full YYYY-MM-DD date is accepted; missing,
    unknown, month-only and year-only publication dates remain None.
    """
    path = Path(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("documents", payload.get("corpus_manifest"))
    if not isinstance(rows, list):
        raise CorpusIngestionError("input.json requires a document metadata list")
    base = Path(base_directory) if base_directory is not None else path.parent
    documents = []
    for row in rows:
        metadata = dict(row)
        raw_path = metadata.pop("path", metadata.pop("file_path", None))
        if raw_path is None:
            raise CorpusIngestionError("document metadata requires a PDF path")
        published = metadata.get("published_at")
        metadata["published_at"] = date.fromisoformat(published) if isinstance(published, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", published) else None
        if "retrieved_at" in metadata:
            metadata["retrieved_at"] = datetime.fromisoformat(metadata["retrieved_at"])
        if "page_count" in metadata:
            metadata["expected_page_count"] = metadata.pop("page_count")
        if "sha256" in metadata:
            metadata["expected_sha256"] = metadata.pop("sha256")
        pdf_path = Path(raw_path)
        metadata["path"] = pdf_path if pdf_path.is_absolute() else base / pdf_path
        documents.append(DocumentInput(**metadata))
    return tuple(documents)


def legacy_corpus_documents(
    corpus_directory: Path | str,
    *,
    domain: Domain = Domain.PHYSICAL_AI_ROBOTICS,
    company_aliases: Mapping[str, Sequence[str]] | None = None,
    include_derived: bool = False,
) -> tuple[DocumentInput, ...]:
    """Adapt the existing checked-in source manifest without changing it.

    Generated summaries are excluded by default.  If explicitly included,
    their origin remains the generated file, and provenance clearly identifies
    them as derived material rather than verified original website content.
    Background papers keep empty company IDs, so another author's experiment
    cannot be returned as the evaluated company's performance evidence.
    """
    corpus_directory = Path(corpus_directory)
    rows = json.loads((corpus_directory / "sources.json").read_text(encoding="utf-8"))
    aliases = {str(key): tuple(value) for key, value in (company_aliases or {}).items()}
    csv_path = corpus_directory.parent / "base/raw/thevc_startups_30_updated.csv"
    if not aliases and csv_path.exists():
        with csv_path.open(encoding="utf-8-sig", newline="") as stream:
            aliases = {row["company_id"]: (row["기업명"],) for row in csv.DictReader(stream)}
    result = []
    for row in rows:
        derived = bool(row.get("is_derived", False)) or row.get("source_type") == "derived_web_summary"
        if derived and not include_derived:
            continue
        ids = tuple(str(value) for value in row.get("company_ids", ()))
        declared_names = row.get("company_names", {})
        names = []
        for company_id in ids:
            declared = declared_names.get(company_id, ())
            if declared:
                chosen = declared[0]
                if company_id in aliases and _name(chosen) not in {_name(alias) for alias in aliases[company_id]}:
                    raise CorpusIngestionError(f"legacy source company identity differs from the company registry: {company_id}")
            elif company_id in aliases and aliases[company_id]:
                chosen = aliases[company_id][0]
            else:
                raise CorpusIngestionError(f"legacy source has no verified company name for {company_id}")
            names.append(chosen)
        raw_type = row.get("source_type", "institution")
        source_type = {"author_technical_report": "paper", "research_paper": "paper",
                       "secondary_business_report": "news", "derived_web_summary": "company"}.get(raw_type, raw_type)
        if source_type not in {"company", "paper", "institution", "news", "dataset"}:
            raise CorpusIngestionError(f"unmapped legacy source type: {raw_type}")
        controlled = raw_type in {"author_technical_report", "derived_web_summary", "company"}
        publisher = row.get("publisher")
        publisher_group = row.get("publisher_group") or (
            "company_" + "_".join(ids) if controlled and ids else
            "publisher_" + hashlib.sha256(_name(publisher or row["document_id"]).encode()).hexdigest()[:20]
        )
        published = row.get("published_date", row.get("published_at"))
        published_at = date.fromisoformat(published) if isinstance(published, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", published) else None
        filename = Path(row["file_name"])
        pdf_path = corpus_directory / filename
        if not pdf_path.exists() and not filename.parts[0] == "raw":
            pdf_path = corpus_directory / "raw" / filename
        result.append(DocumentInput(
            path=pdf_path, document_id=row["document_id"], source_id=row["document_id"],
            company_ids=ids, company_names=tuple(names), domain=domain,
            publisher_group=publisher_group, title=row["title"], publisher=publisher,
            source_type=source_type, corporate_controlled=controlled,
            original_url=None if derived else row.get("source_url"),
            published_at=published_at, language=row.get("language"),
            expected_sha256=row.get("sha256"), expected_page_count=row.get("page_count"),
            evidence_page_numbers=tuple(row["company_evidence_pages"]) if "company_evidence_pages" in row else None,
            page_excerpts={int(number): (markers["start"], markers["end"]) for number, markers in row.get("chunk_excerpt_by_page", {}).items()},
            is_derived=derived, limitations=row.get("limitations"), evidence_scope=row.get("evidence_scope"),
            publication_label=str(published) if published is not None else str(row["published_year"]) if "published_year" in row else None,
            retrieved_label=row.get("retrieved_date"),
        ))
    return tuple(result)
