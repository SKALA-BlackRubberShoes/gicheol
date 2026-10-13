"""Render evaluated neutral content without changing it, then inspect PDF bytes."""
from __future__ import annotations

from pathlib import Path
import re
import unicodedata

from main.agents.supervisor import contracts as c
from .renderers import _render_pdf, render_markdown


class PdfLayoutError(RuntimeError):
    """A staging candidate failed layout or rendered-content validation."""


def report_document(report: c.DraftReport) -> dict:
    report = c.DraftReport.model_validate(report.model_dump(mode="json"))
    citations = {}
    for item in report.citations:
        citations.setdefault(item.paragraph_id, []).append(f"[{item.source_id} | {item.evidence_id}]")

    def paragraph_text(paragraph: c.ReportParagraph) -> str:
        markers = citations.get(paragraph.paragraph_id, [])
        return paragraph.text + (" " + " ".join(markers) if markers else "")

    return {"title": "", "subtitle": "",
        "summary": "\n\n".join(paragraph_text(p) for p in report.sections[0].paragraphs),
        "sections": [{"title": section.title, "paragraphs": [paragraph_text(p) for p in section.paragraphs]}
                     for section in report.sections[1:]]}


def neutral_markdown(report: c.DraftReport) -> str:
    # The existing renderer adds a title; the DTO's first heading is SUMMARY.
    return render_markdown(report_document(report)).removeprefix("# \n\n")


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFC", text))


def validate_pdf(path: Path, report: c.DraftReport, *, summary_page_fraction: float) -> dict:
    """Verify A4, embedded font, physical limits and complete ordered body text."""
    from pypdf import PdfReader

    reader = PdfReader(path)
    if not 1 <= len(reader.pages) <= 10 or not 0 < summary_page_fraction <= 0.5:
        raise PdfLayoutError("PDF_LAYOUT_FAILED: page count or SUMMARY height exceeded")
    actual = []
    for page_index, page in enumerate(reader.pages):
        if abs(float(page.mediabox.width) - 595.2756) > 0.1 or abs(float(page.mediabox.height) - 841.8898) > 0.1 or page.rotation:
            raise PdfLayoutError("PDF_LAYOUT_FAILED: report must use unrotated A4 pages")
        for font_ref in page["/Resources"]["/Font"].values():
            font = font_ref.get_object()
            if font.get("/Subtype") == "/TrueType":
                descriptor = font.get("/FontDescriptor")
                if descriptor is None or not descriptor.get_object().get("/FontFile2") or not font.get("/ToUnicode"):
                    raise PdfLayoutError("PDF_LAYOUT_FAILED: font must be embedded with Unicode mapping")
        embedded = any(font_ref.get_object().get("/FontDescriptor") and
                       font_ref.get_object()["/FontDescriptor"].get_object().get("/FontFile2")
                       for font_ref in page["/Resources"]["/Font"].values())
        if not embedded:
            raise PdfLayoutError("PDF_LAYOUT_FAILED: embedded Korean font missing")

        def visitor(text, cm, tm, font_dict, font_size):
            # The only added text is the renderer's 8pt page number at y=20.
            y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
            if font_size == 8 and y < 42 and text.strip() == str(page_index + 1):
                return
            actual.append(text)

        page.extract_text(visitor_text=visitor)
    document = report_document(report)
    expected = ["SUMMARY", document["summary"]]
    for section in document["sections"]:
        expected.extend([section["title"], *section["paragraphs"]])
    if _compact("".join(actual)) != _compact("".join(expected)):
        raise PdfLayoutError("PDF_LAYOUT_FAILED: PDF text or citations differ from the evaluated draft")
    return {"page_count": len(reader.pages), "summary_page_fraction": summary_page_fraction,
            "pdf_validation_pass": True}


def render_neutral_pdf(report: c.DraftReport, path: Path, *, font_path: str) -> dict:
    """Only layout changes: no truncation, model call or automatic rewriting."""
    if not font_path:
        raise PdfLayoutError("PDF_LAYOUT_FAILED: configured REPORT_FONT_PATH is required")
    metrics = {}
    try:
        _render_pdf(report_document(report), path, font_path=font_path, max_pages=10,
                    page_limit=10, author="Neutral Startup Evaluation", metrics=metrics)
        return validate_pdf(path, report, summary_page_fraction=metrics["summary_page_fraction"])
    except PdfLayoutError:
        raise
    except Exception as exc:
        raise PdfLayoutError(f"PDF_LAYOUT_FAILED: {exc}") from exc
