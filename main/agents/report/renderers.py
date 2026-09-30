"""Render a report document as Markdown or an embedded-font Korean PDF.

The renderer accepts plain text, never trusted HTML. It preserves all submitted
content and fails explicitly when the assignment's physical limits are exceeded.
"""

from __future__ import annotations

import hashlib
import html
import io
import math
import os
from pathlib import Path
import re
import tempfile
import unicodedata
from typing import Any


_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/AppleMyungjo.ttf",
    "/Library/Fonts/NanumGothic.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/usr/share/fonts/truetype/nanum/NanumMyeongjo.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansKR-Regular.ttf",
    "C:/Windows/Fonts/malgun.ttf",
)


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be plain text (str).")
    # macOS paths may contain decomposed Hangul; compose display text so the
    # embedded font renders syllables correctly. Raw audit input stays intact.
    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))


def _validate_document(document: dict) -> None:
    if not isinstance(document, dict):
        raise ValueError("document must be a dictionary.")
    for field in ("title", "subtitle", "summary"):
        _text(document.get(field, ""), field)
    sections = document.get("sections", [])
    if not isinstance(sections, list):
        raise ValueError("sections must be a list.")
    for index, section in enumerate(sections):
        label = f"sections[{index}]"
        if not isinstance(section, dict):
            raise ValueError(f"{label} must be a dictionary.")
        _text(section.get("title", ""), f"{label}.title")
        for field in ("paragraphs", "bullets"):
            entries = section.get(field, [])
            if not isinstance(entries, list):
                raise ValueError(f"{label}.{field} must be a list.")
            for entry in entries:
                _text(entry, f"{label}.{field}")
        if "emphasis" in section:
            emphasis = section["emphasis"]
            if not isinstance(emphasis, list) or len(emphasis) != len(section.get("paragraphs", [])):
                raise ValueError(f"{label}.emphasis must match paragraphs.")
            for entry, text in zip(emphasis, section.get("paragraphs", [])):
                _text(entry, f"{label}.emphasis")
                if entry and entry not in text:
                    raise ValueError(f"{label}.emphasis must refer to existing plain text.")
        tables = section.get("tables", [])
        if not isinstance(tables, list):
            raise ValueError(f"{label}.tables must be a list.")
        for table in tables:
            if not isinstance(table, dict):
                raise ValueError(f"{label}.tables entries must be dictionaries.")
            headers, rows = table.get("headers"), table.get("rows")
            if not isinstance(headers, list) or not headers:
                raise ValueError(f"{label}: every table needs a non-empty headers list.")
            if not isinstance(rows, list):
                raise ValueError(f"{label}: table rows must be a list.")
            if "column_weights" in table:
                weights = table["column_weights"]
                if not isinstance(weights, list) or len(weights) != len(headers):
                    raise ValueError(f"{label}: column_weights must be a list matching the header count.")
                if any(isinstance(weight, bool) or not isinstance(weight, (int, float))
                       or not math.isfinite(weight) or weight <= 0 for weight in weights):
                    raise ValueError(f"{label}: column_weights must contain finite positive numbers.")
            for header in headers:
                _text(header, f"{label}.tables.headers")
            for row in rows:
                if not isinstance(row, list) or len(row) != len(headers):
                    raise ValueError(f"{label}: each table row must match the header count.")
                for cell in row:
                    _text(cell, f"{label}.tables.rows")


def _markdown_text(value: str, *, table: bool = False) -> str:
    text = html.escape(_text(value, "text"), quote=False)
    text = re.sub(r"([\\`*_\[\]{}()#+.!|\-])", r"\\\1", text)
    return text.replace("\n", "<br>" if table else "  \n")


def render_markdown(document: dict) -> str:
    """Return readable Markdown, escaping source markup and table delimiters."""
    _validate_document(document)
    chunks = [f"# {_markdown_text(document.get('title', ''))}"]
    if document.get("subtitle"):
        chunks.append(_markdown_text(document["subtitle"]))
    chunks.extend(["## SUMMARY", _markdown_text(document.get("summary", ""))])
    for section in document.get("sections", []):
        chunks.append(f"## {_markdown_text(section.get('title', ''))}")
        for index, item in enumerate(section.get("paragraphs", [])):
            emphasis = section.get("emphasis", [""] * len(section.get("paragraphs", [])))[index]
            if emphasis:
                before, _, after = item.partition(emphasis)
                chunks.append(_markdown_text(before) + "*" + _markdown_text(emphasis) + "*" + _markdown_text(after))
            else:
                chunks.append(_markdown_text(item))
        if section.get("bullets"):
            chunks.append("\n".join(f"- {_markdown_text(item).replace(chr(10), chr(10) + '  ')}"
                                    for item in section["bullets"]))
        for table in section.get("tables", []):
            def row_line(row: list[str]) -> str:
                return "| " + " | ".join(_markdown_text(cell, table=True) for cell in row) + " |"

            lines = [row_line(table["headers"]),
                     "| " + " | ".join("---" for _ in table["headers"]) + " |"]
            lines.extend(row_line(row) for row in table["rows"])
            chunks.append("\n".join(lines))
    return "\n\n".join(chunks).rstrip() + "\n"


def _register_korean_font(font_path: str | None) -> str:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    explicit = font_path if font_path is not None else os.environ.get("REPORT_FONT_PATH")
    candidates = [explicit] if explicit else list(_FONT_CANDIDATES)
    errors: list[str] = []
    for candidate in candidates:
        path = Path(candidate).expanduser()
        if not path.is_file():
            errors.append(f"{path}: file not found")
            continue
        # Stable names avoid conflicting registrations from multiple reports.
        name = "ReportKorean_" + hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:12]
        try:
            if name not in pdfmetrics.getRegisteredFontNames():
                font = TTFont(name, str(path))
                if not all(ord(char) in font.face.charToGlyph for char in "한글보고서"):
                    raise ValueError("font does not contain the required Korean glyphs")
                pdfmetrics.registerFont(font)
            return name
        except Exception as exc:
            errors.append(f"{path}: {exc}")
    detail = "; ".join(errors) if explicit else "No supported Korean TrueType font was found."
    raise ValueError(
        "Korean PDF font unavailable. Set REPORT_FONT_PATH or pass font_path to an "
        "embeddable Korean .ttf font such as NanumGothic.ttf. " + detail
    )


def render_pdf(
    document: dict,
    path: Path,
    *,
    font_path: str | None = None,
    max_pages: int = 5,
) -> int:
    """Atomically write an A4 PDF and return its page count.

    SUMMARY, including its heading, may occupy at most half the usable page.
    The title, subtitle, and complete SUMMARY must fit together on the first page.
    Tables may supply positive ``column_weights`` matching their header count.
    ``max_pages`` can tighten the five-page assignment limit, but cannot relax it.
    Overflow or invalid input raises ValueError before an existing file is changed.
    An embedded Korean TrueType font is required to keep PDFs portable.
    """
    _validate_document(document)
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 5:
        raise ValueError("max_pages must be an integer from 1 to 5.")
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.pdfgen.canvas import Canvas
        from reportlab.platypus import (
            Flowable, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
        )
        from reportlab.platypus.doctemplate import LayoutError
    except ImportError as exc:
        raise RuntimeError("PDF rendering requires reportlab. Install main/agents/report/requirements.txt.") from exc

    font_name = _register_korean_font(font_path)
    width, height = A4
    margin_x, margin_top, margin_bottom = 40, 42, 42
    usable_width = width - 2 * margin_x
    usable_height = height - margin_top - margin_bottom
    # SimpleDocTemplate's frame has six points of padding on each side.
    content_width = usable_width - 12
    ink, muted = colors.HexColor("#182A3B"), colors.HexColor("#586B7C")
    body = ParagraphStyle(
        "ReportBody", fontName=font_name, fontSize=9.5, leading=14,
        textColor=ink, spaceAfter=6, wordWrap="CJK", splitLongWords=1,
        alignment=TA_LEFT, allowWidows=0, allowOrphans=0,
    )
    title_style = ParagraphStyle("ReportTitle", parent=body, fontSize=19, leading=25, spaceAfter=7)
    subtitle_style = ParagraphStyle("ReportSubtitle", parent=body, fontSize=9, leading=13,
                                    textColor=muted, spaceAfter=12)
    heading_style = ParagraphStyle("ReportHeading", parent=body, fontSize=12, leading=17,
                                   spaceBefore=9, spaceAfter=7, keepWithNext=1)
    cell_style = ParagraphStyle("ReportCell", parent=body, fontSize=8.5, leading=12, spaceAfter=0,
                                allowWidows=1, allowOrphans=1)
    header_style = ParagraphStyle("ReportTableHeader", parent=cell_style, textColor=colors.white)
    bullet_style = ParagraphStyle("ReportBullet", parent=body, leftIndent=9, firstLineIndent=-7)

    def paragraph(text: str, style: Any = body) -> Any:
        return Paragraph(html.escape(_text(text, "text"), quote=False).replace("\n", "<br/>"), style)

    class ReferenceParagraph(Flowable):
        """안전한 평문을 줄바꿈하고 지정된 서지 항목만 기울인다.

        한글 TTF에는 italic 변형이 없는 경우가 많아 글꼴을 바꾸지 않고
        지정 문자열의 그리기 좌표만 기울인다. 원문 HTML은 해석하지 않는다.
        """
        def __init__(self, text="", emphasis="", lines=None):
            super().__init__()
            self.text = _text(text, "reference")
            self.emphasis = _text(emphasis, "reference emphasis")
            self.lines = lines
            self.spaceAfter = body.spaceAfter

        def wrap(self, available_width, available_height):
            from reportlab.pdfbase.pdfmetrics import stringWidth
            if self.lines is None:
                start = self.text.find(self.emphasis) if self.emphasis else -1
                end = start + len(self.emphasis)
                self.lines, line, used = [], [], 0.0
                for index, char in enumerate(self.text):
                    char_width = stringWidth(char, font_name, body.fontSize)
                    if char == "\n" or used + char_width > available_width - 3:
                        self.lines.append(line)
                        line, used = [], 0.0
                        if char == "\n":
                            continue
                    italic = start <= index < end if start >= 0 else False
                    if line and line[-1][1] == italic:
                        line[-1] = (line[-1][0] + char, italic)
                    else:
                        line.append((char, italic))
                    used += char_width
                if line:
                    self.lines.append(line)
            self.width = available_width
            self.height = len(self.lines) * body.leading
            return self.width, self.height

        def split(self, available_width, available_height):
            self.wrap(available_width, available_height)
            count = int(available_height // body.leading)
            if count < 1:
                return []
            if count >= len(self.lines):
                return [self]
            first = ReferenceParagraph(lines=self.lines[:count])
            first.spaceAfter = 0
            return [first, ReferenceParagraph(lines=self.lines[count:])]

        def draw(self):
            from reportlab.pdfbase.pdfmetrics import stringWidth
            canvas = self.canv
            y = self.height - body.fontSize
            for line in self.lines:
                x = 0.0
                for text, italic in line:
                    canvas.saveState()
                    canvas.translate(x, y)
                    if italic:
                        canvas.transform(1, 0, math.tan(math.radians(12)), 1, 0, 0)
                    canvas.setFont(font_name, body.fontSize)
                    canvas.setFillColor(ink)
                    canvas.drawString(0, 0, text)
                    canvas.restoreState()
                    x += stringWidth(text, font_name, body.fontSize)
                y -= body.leading

    summary = [paragraph("SUMMARY", heading_style), paragraph(document.get("summary", ""))]
    summary_height = sum(item.wrap(content_width, usable_height)[1]
                         + item.getSpaceBefore() + item.getSpaceAfter() for item in summary)
    if summary_height > usable_height / 2:
        raise ValueError(
            f"SUMMARY exceeds half of the usable A4 page ({summary_height:.0f} pt > "
            f"{usable_height / 2:.0f} pt). Shorten the summary and move detailed evidence "
            "into the body sections. No output PDF was replaced."
        )

    opening: list[Any] = []
    if document.get("title"):
        opening.append(paragraph(document["title"], title_style))
    if document.get("subtitle"):
        opening.append(paragraph(document["subtitle"], subtitle_style))
    opening.extend(summary)
    opening_height = sum(item.wrap(content_width, usable_height)[1]
                         + item.getSpaceBefore() + item.getSpaceAfter() for item in opening)
    if opening_height > usable_height - 12:
        raise ValueError(
            "The title, subtitle, and SUMMARY cannot fit together on the first page. "
            "Shorten the title or subtitle so the complete SUMMARY remains on page 1. "
            "No output PDF was replaced."
        )
    story: list[Any] = [KeepTogether(opening)]
    for section in document.get("sections", []):
        story.append(paragraph(section.get("title", ""), heading_style))
        for index, text in enumerate(section.get("paragraphs", [])):
            emphasis = section.get("emphasis", [""] * len(section.get("paragraphs", [])))[index]
            story.append(ReferenceParagraph(text, emphasis) if emphasis else paragraph(text))
        story.extend(paragraph("- " + text, bullet_style) for text in section.get("bullets", []))
        for table in section.get("tables", []):
            count = len(table["headers"])
            data = [[paragraph(cell, header_style) for cell in table["headers"]]]
            data.extend([paragraph(cell, cell_style) for cell in row] for row in table["rows"])
            weights = table.get("column_weights", [1] * count)
            # Normalize first to avoid overflowing the sum of large valid weights.
            largest = max(weights)
            normalized = [weight / largest for weight in weights]
            total = math.fsum(normalized)
            column_widths = [content_width * weight / total for weight in normalized]
            grid = Table(data, colWidths=column_widths,
                         repeatRows=1, hAlign="LEFT", splitByRow=1)
            grid.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#243E54")),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F0F4F7")]),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 6),
                ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LINEBELOW", (0, 0), (-1, 0), 0.4, colors.HexColor("#243E54")),
                ("LINEBELOW", (0, 1), (-1, -1), 0.25, colors.HexColor("#D4DEE5")),
            ]))
            story.extend([grid, Spacer(1, 9)])

    page_count = 0

    class PageLimitCanvas(Canvas):
        def showPage(self) -> None:
            nonlocal page_count
            page_count += 1
            if page_count > max_pages:
                raise ValueError(
                    f"Report exceeds the {max_pages}-page limit. Shorten the report text, "
                    "consolidate repeated evidence, or reduce table rows. "
                    "No content was silently removed and no output PDF was replaced."
                )
            super().showPage()

    def draw_footer(canvas: Any, doc: Any) -> None:
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor("#D4DEE5"))
        canvas.setLineWidth(0.4)
        canvas.line(margin_x + 6, 32, width - margin_x - 6, 32)
        canvas.setFont(font_name, 8)
        canvas.setFillColor(muted)
        canvas.drawRightString(width - margin_x - 6, 20, str(doc.page))
        canvas.restoreState()

    buffer = io.BytesIO()
    report = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=margin_x, rightMargin=margin_x,
        topMargin=margin_top, bottomMargin=margin_bottom,
        title=document.get("title", ""), author="Startup Investment Report", pageCompression=1,
    )
    try:
        report.build(story, onFirstPage=draw_footer, onLaterPages=draw_footer,
                     canvasmaker=PageLimitCanvas)
    except LayoutError as exc:
        raise ValueError(
            "A report block cannot fit on an A4 page. Shorten an oversized table cell, "
            "split the table into smaller rows, or shorten the title. "
            "No output PDF was replaced."
        ) from exc

    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix=f".{output.name}.",
                                         suffix=".tmp", delete=False) as stream:
            temporary = stream.name
            stream.write(buffer.getvalue())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)
    return page_count
