"""Behavioral checks for markup safety and the physical submission constraints."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from main.agents.report.renderers import render_markdown, render_pdf


def example_document() -> dict:
    return {
        "title": "기업 분석 보고서",
        "subtitle": "근거와 한계를 함께 기록한 보고서",
        "summary": "검증한 자료를 바탕으로 기업의 주요 사업과 위험을 정리했습니다.",
        "sections": [
            {
                "title": "분석 결과",
                "paragraphs": ["본문 <script>alert('literal')</script> & 근거", "두 번째 문단입니다."],
                "bullets": ["확인이 필요한 사항을 표시합니다."],
                "tables": [{
                    "headers": ["항목", "확인 내용"],
                    "rows": [["자료 | 1", "첫째 줄\n둘째 줄"],
                             ["URL", "https://example.com/" + "long-evidence-id" * 18]],
                }],
            },
            {"title": "REFERENCE", "paragraphs": ["[1] 공시자료, 확인 일자 2026-09-30."],
             "bullets": [], "tables": []},
        ],
    }


class MarkdownTests(unittest.TestCase):
    def test_raw_html_and_table_delimiters_are_escaped(self) -> None:
        result = render_markdown(example_document())
        self.assertIn("## SUMMARY", result)
        self.assertIn("## REFERENCE", result)
        self.assertLess(result.index("## SUMMARY"), result.index("## 분석 결과"))
        self.assertIn("&lt;script&gt;", result)
        self.assertNotIn("<script>", result)
        self.assertIn(r"자료 \| 1", result)
        self.assertIn("첫째 줄<br>둘째 줄", result)

    def test_invalid_table_does_not_drop_extra_cells(self) -> None:
        document = example_document()
        document["sections"][0]["tables"][0]["rows"].append(["하나", "둘", "셋"])
        with self.assertRaisesRegex(ValueError, "header count"):
            render_markdown(document)

    def test_column_weights_reject_mismatches_and_nonpositive_or_nonfinite_values(self) -> None:
        invalid = ([1], [1, 2, 3], None, [0, 1], [-1, 1], ["1", 1],
                   [True, 1], [float("inf"), 1], [float("nan"), 1])
        for weights in invalid:
            with self.subTest(weights=weights):
                document = example_document()
                document["sections"][0]["tables"][0]["column_weights"] = weights
                with self.assertRaisesRegex(ValueError, "column_weights"):
                    render_markdown(document)


class PdfTests(unittest.TestCase):
    def setUp(self) -> None:
        # Keep test-created artifacts under outputs/reports/tmp.
        scratch = Path(__file__).resolve().parents[4] / "outputs/reports/tmp"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="render-test-", dir=scratch)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "report.pdf"

    def test_korean_literal_xml_multiline_table_and_long_url(self) -> None:
        from pypdf import PdfReader

        document = example_document()
        original = deepcopy(document)
        count = render_pdf(document, self.path)
        reader = PdfReader(self.path)
        self.assertEqual(count, len(reader.pages))
        self.assertLessEqual(count, 5)
        text = "\n".join(page.extract_text() for page in reader.pages)
        self.assertIn("기업 분석 보고서", text)
        self.assertIn("<script>", text)
        self.assertIn("첫째 줄", text)
        self.assertIn("둘째 줄", text)
        self.assertIn("REFERENCE", text)
        self.assertIn("long-evidence-id" * 18, text.replace("\n", ""))
        self.assertEqual(document, original)
        self.assertAlmostEqual(float(reader.pages[0].mediabox.width), 595.2756, places=2)
        embedded = []
        for font in reader.pages[0]["/Resources"]["/Font"].values():
            descriptor = font.get_object().get("/FontDescriptor")
            if descriptor:
                embedded.append(descriptor.get_object().get("/FontFile2") is not None)
        self.assertIn(True, embedded, "Korean font must be embedded in the PDF")

    def test_summary_half_page_limit_and_existing_output_is_preserved(self) -> None:
        self.path.write_bytes(b"previous report")
        document = example_document()
        document["summary"] = "요약 분량 제한을 확인하는 문장입니다. " * 400
        with self.assertRaisesRegex(ValueError, "SUMMARY exceeds half"):
            render_pdf(document, self.path)
        self.assertEqual(self.path.read_bytes(), b"previous report")

    def test_oversized_title_or_subtitle_cannot_push_summary_off_first_page(self) -> None:
        self.path.write_bytes(b"previous report")
        for field in ("title", "subtitle"):
            with self.subTest(field=field):
                document = example_document()
                document[field] = "길어진 머리말 줄입니다.\n" * 70
                with self.assertRaisesRegex(ValueError, "SUMMARY cannot fit together on the first page"):
                    render_pdf(document, self.path)
                self.assertEqual(self.path.read_bytes(), b"previous report")

    def test_summary_remains_on_first_page_with_multiline_title(self) -> None:
        from pypdf import PdfReader

        document = example_document()
        document["title"] = "여러 줄 제목\n" * 18
        render_pdf(document, self.path)
        first_page = PdfReader(self.path).pages[0].extract_text()
        self.assertIn("SUMMARY", first_page)
        self.assertIn(document["summary"], first_page)

    def test_weighted_table_preserves_text_with_large_valid_weights(self) -> None:
        from pypdf import PdfReader

        document = example_document()
        document["sections"][0]["tables"][0]["column_weights"] = [1e308, 1.5e308]
        render_pdf(document, self.path)
        text = "\n".join(page.extract_text() for page in PdfReader(self.path).pages)
        self.assertIn("첫째 줄", text)
        self.assertIn("long-evidence-id" * 18, text.replace("\n", ""))

    def test_page_overflow_and_existing_output_is_preserved(self) -> None:
        self.path.write_bytes(b"previous report")
        document = example_document()
        document["sections"][0]["paragraphs"] = ["반복 근거 문장입니다. " * 12] * 150
        with self.assertRaisesRegex(ValueError, "5-page limit"):
            render_pdf(document, self.path)
        self.assertEqual(self.path.read_bytes(), b"previous report")
        self.assertEqual(list(Path(self.temp.name).iterdir()), [self.path])

    def test_repeated_table_headers_across_multiple_pages(self) -> None:
        from pypdf import PdfReader

        document = example_document()
        document["sections"][0]["tables"][0]["rows"] = [[f"자료 {i}", "확인한 근거"] for i in range(65)]
        count = render_pdf(document, self.path)
        reader = PdfReader(self.path)
        self.assertGreater(count, 1)
        for page in reader.pages:
            text = page.extract_text()
            if "자료 " in text:
                self.assertIn("확인 내용", text)

    def test_invalid_explicit_font_has_actionable_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "REPORT_FONT_PATH"):
            render_pdf(example_document(), self.path, font_path=str(Path(self.temp.name) / "missing.ttf"))
        self.assertFalse(self.path.exists())

    def test_submission_limit_cannot_be_raised(self) -> None:
        with self.assertRaisesRegex(ValueError, "1 to 5"):
            render_pdf(example_document(), self.path, max_pages=6)


if __name__ == "__main__":
    unittest.main()
