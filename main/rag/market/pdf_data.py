"""PDF 텍스트 추출·청킹과 문서 메타데이터 대응입니다."""

from __future__ import annotations

import json
import re
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5
from .models import MarketChunk, MarketDocumentSource, MarketRAGDataError


def _clean_page_text(text: str) -> str:
    """PDF 추출 과정에서 생기는 과도한 공백을 읽기 쉬운 형태로 정리합니다."""

    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _chunk_page_text(
    text: str,
    *,
    max_chars: int = 1_800,
    overlap_chars: int = 200,
) -> list[str]:
    """한 페이지를 문자 수 기준의 겹치는 청크로 나눕니다.

    PDF마다 한국어·영어의 토큰 비율이 다르므로 청킹 자체는 단순하고 재현 가능한
    문자 수 기준으로 수행합니다. 임베딩 API의 실제 토큰 한도는 임베더가 별도로
    확인합니다. 청크는 한 페이지를 넘지 않으므로 검색 결과의 페이지 인용이
    항상 명확합니다.
    """

    if max_chars < 200:
        raise ValueError("max_chars must be at least 200")
    if overlap_chars < 0 or overlap_chars >= max_chars:
        raise ValueError("overlap_chars must be >= 0 and smaller than max_chars")

    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        hard_end = min(start + max_chars, len(text))
        end = hard_end

        # 가능한 경우 문장 또는 줄의 끝에서 자릅니다. 너무 앞에서 잘려 청크가
        # 지나치게 작아지는 경우에는 원래의 hard_end를 그대로 사용합니다.
        if hard_end < len(text):
            candidates = [
                text.rfind(separator, start + max_chars // 2, hard_end)
                for separator in ("\n", ". ", "다. ", "요. ")
            ]
            boundary = max(candidates)
            if boundary > start:
                end = boundary + 1

        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(end - overlap_chars, start + 1)

    return chunks


def _load_manifest(path: Path, pdf_dir: Path) -> list[MarketDocumentSource]:
    """sources.json을 검증하고 실제 PDF 파일과 1:1로 대응시킵니다."""

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MarketRAGDataError(
            f"Cannot read market source manifest {path}: {exc}"
        ) from exc

    if not isinstance(raw, list) or not raw:
        raise MarketRAGDataError("Market source manifest must be a nonempty JSON array")

    try:
        sources = [MarketDocumentSource.model_validate(item) for item in raw]
    except Exception as exc:
        raise MarketRAGDataError(f"Invalid market source metadata: {exc}") from exc

    ids = [source.document_id for source in sources]
    names = [source.file_name for source in sources]
    if len(ids) != len(set(ids)):
        raise MarketRAGDataError("document_id values in sources.json must be unique")
    if len(names) != len(set(names)):
        raise MarketRAGDataError("file_name values in sources.json must be unique")

    missing = [name for name in names if not (pdf_dir / name).is_file()]
    if missing:
        raise MarketRAGDataError(
            f"PDF files listed in sources.json are missing: {missing}"
        )

    # manifest에 등록되지 않은 PDF를 조용히 무시하지 않습니다. 새 문서를 추가한
    # 팀원이 출처 URL과 발행기관도 함께 기록하도록 하기 위한 검사입니다.
    actual = {path.name for path in pdf_dir.glob("*.pdf")}
    unregistered = sorted(actual - set(names))
    if unregistered:
        raise MarketRAGDataError(
            f"PDF files are not registered in sources.json: {unregistered}"
        )

    return sources


def prepare_chunks(
    sources: list[MarketDocumentSource], pdf_dir: Path
) -> list[MarketChunk]:
    """페이지는 저장한 PDF 내부의 1-based 번호입니다. 원본 보고서 페이지와 다를 수 있습니다."""
    from pypdf import PdfReader

    chunks: list[MarketChunk] = []

    for source in sources:
        pdf_path = pdf_dir / source.file_name
        try:
            reader = PdfReader(str(pdf_path))
        except Exception as exc:
            raise MarketRAGDataError(f"Cannot read PDF {pdf_path}: {exc}") from exc

        for page_number, page in enumerate(reader.pages, start=1):
            try:
                page_text = _clean_page_text(page.extract_text() or "")
            except Exception as exc:
                raise MarketRAGDataError(
                    f"Cannot extract {source.file_name} page {page_number}: {exc}"
                ) from exc

            # 표지만 이미지인 문서 등은 빈 페이지일 수 있습니다. 검색에
            # 사용할 텍스트가 없으면 청크를 만들지 않습니다.
            for chunk_index, text in enumerate(_chunk_page_text(page_text)):
                stable_key = (
                    f"market-rag:{source.document_id}:{page_number}:"
                    f"{chunk_index}:{text}"
                )
                chunks.append(
                    MarketChunk(
                        chunk_id=str(uuid5(NAMESPACE_URL, stable_key)),
                        document_id=source.document_id,
                        title=source.title,
                        publisher=source.publisher,
                        published_year=source.published_year,
                        source_url=source.source_url,
                        source_file=source.file_name,
                        page=page_number,
                        chunk_index=chunk_index,
                        region=source.region,
                        language=source.language,
                        market_segments=source.market_segments,
                        text=text,
                    )
                )

    if not chunks:
        raise MarketRAGDataError(
            "No searchable text was extracted from the PDF directory"
        )
    return chunks
