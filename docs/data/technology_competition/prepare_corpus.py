"""Local PDF extraction only; no model, embedding API, or network calls."""
import hashlib
import json
import logging
from pathlib import Path

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent


class WarningLog(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


def main():
    sources = json.loads((ROOT / "sources.json").read_text())
    output = ROOT / "processed"
    output.mkdir(exist_ok=True)
    reports = []
    total_chunks = 0
    warnings = WarningLog()
    logging.getLogger("pypdf").addHandler(warnings)
    with (output / "pages.jsonl").open("w", encoding="utf-8") as pages_file, (
        output / "chunks.jsonl"
    ).open("w", encoding="utf-8") as chunks_file:
        for source in sources:
            warnings.messages.clear()
            path = ROOT / source["file_name"]
            data = path.read_bytes()
            if not data.startswith(b"%PDF-"):
                raise ValueError(f"Not a PDF: {path}")
            reader = PdfReader(path)
            page_lengths = []
            chunk_count = 0
            for number, page in enumerate(reader.pages, 1):
                text = (page.extract_text() or "").strip()
                # Normalize PDF line separators before JSONL serialization.
                for separator in ("\u2028", "\u2029", "\x0b", "\x0c", "\x85", "\x1c", "\x1d", "\x1e"):
                    text = text.replace(separator, "\n")
                page_lengths.append(len(text))
                company_ids = source["company_ids"]
                scope = source["evidence_scope"]
                if number not in source.get("company_evidence_pages", [number]):
                    company_ids = []
                    scope = "other_business_context"
                metadata = {
                    "document_id": source["document_id"],
                    "file_name": source["file_name"],
                    "title": source["title"],
                    "page": number,
                    "source_url": source.get("source_urls_by_page", {}).get(str(number), source["source_url"]),
                    "company_ids": company_ids,
                    "evidence_scope": scope,
                    "source_type": source["source_type"],
                    "is_derived": source["is_derived"],
                    "published_date": source["published_date"],
                    "retrieved_date": source["retrieved_date"],
                    "limitations": source["limitations"],
                }
                pages_file.write(json.dumps({**metadata, "text": text}, ensure_ascii=False) + "\n")
                if scope == "other_business_context":
                    continue
                offset = 0
                excerpt = source.get("chunk_excerpt_by_page", {}).get(str(number))
                if excerpt:
                    offset = text.index(excerpt["start"])
                    stop = text.index(excerpt["end"], offset)
                    text = text[offset:stop]
                metadata["is_page_excerpt"] = bool(excerpt)
                # Starter chunks: 1,400 characters / 200 overlap; never cross a page.
                for start in range(0, len(text), 1200):
                    end = min(start + 1400, len(text))
                    row = {
                        **metadata,
                        "chunk_id": f'{source["document_id"]}:p{number}:c{start}',
                        "char_start": offset + start,
                        "char_end": offset + end,
                        "text": text[start:end],
                    }
                    chunks_file.write(json.dumps(row, ensure_ascii=False) + "\n")
                    chunk_count += 1
                    if end == len(text):
                        break
            total_chunks += chunk_count
            reports.append({
                "document_id": source["document_id"],
                "file_name": source["file_name"],
                "bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "pages": len(reader.pages),
                "text_characters": sum(page_lengths),
                "empty_pages": [i for i, n in enumerate(page_lengths, 1) if n == 0],
                "short_text_pages": [i for i, n in enumerate(page_lengths, 1) if n < 100],
                "chunks": chunk_count,
                "parser_warnings": list(warnings.messages),
            })
    report = {
        "pdf_count": len(reports),
        "total_pages": sum(r["pages"] for r in reports),
        "total_chunks": total_chunks,
        "extraction": "pypdf plain text; tables, formulas and reading order need source-page verification",
        "documents": reports,
    }
    (output / "validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
