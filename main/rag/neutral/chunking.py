"""Token-bounded PDF chunks whose quotations remain exact page substrings.

These private RAG records are not replacements for the Supervisor DTOs.  All
external results use Source/Evidence/ArtifactRef from supervisor.contracts.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from typing import Callable, Protocol, Sequence

from main.agents.supervisor.contracts import Domain


class Tokenizer(Protocol):
    def encode(self, text: str) -> Sequence[int]: ...


class TiktokenTokenizer:
    """Local, actual tokenizer with UTF-8-safe original-character offsets.

    A model tokenizer can be injected instead.  Additional model token counts
    can be supplied to chunk_pages when comparing embedding candidates.
    """

    def __init__(self, encoding_name: str = "cl100k_base") -> None:
        import tiktoken

        self.encoding = tiktoken.get_encoding(encoding_name)

    def encode(self, text: str) -> Sequence[int]:
        return self.encoding.encode(text, disallowed_special=())

    def token_offsets(self, text: str) -> Sequence[tuple[int, int]]:
        encoded = text.encode("utf-8")
        # Some tokenizer IDs contain only part of a Korean UTF-8 character.
        # Map every byte to its original character, rather than decoding a
        # token window and accidentally introducing replacement characters.
        byte_char: list[int] = []
        for index, character in enumerate(text):
            byte_char.extend([index] * len(character.encode("utf-8")))
        offset = 0
        spans = []
        for token in self.encode(text):
            piece = self.encoding.decode_single_token_bytes(token)
            end = offset + len(piece)
            if encoded[offset:end] != piece:
                raise ValueError("tokenizer bytes do not match the original text")
            spans.append((byte_char[offset], byte_char[end - 1] + 1))
            offset = end
        return spans


@dataclass(frozen=True)
class DocumentPage:
    document_id: str
    source_id: str
    company_ids: tuple[str, ...]
    domain: Domain
    publisher_group: str
    page_number: int
    text: str
    original_sha256: str
    published_at: date | None = None
    event_date: date | None = None
    language: str | None = None
    char_offset: int = 0


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    document_id: str
    source_id: str
    company_ids: tuple[str, ...]
    domain: Domain
    publisher_group: str
    page_number: int
    text: str
    char_start: int
    char_end: int
    token_count: int
    original_sha256: str
    published_at: date | None = None
    event_date: date | None = None
    language: str | None = None


def _count(tokenizer: Tokenizer, text: str) -> int:
    return len(tokenizer.encode(text))


def _offsets(tokenizer: Tokenizer, text: str) -> Sequence[tuple[int, int]]:
    method = getattr(tokenizer, "token_offsets", None)
    if method is not None:
        spans = list(method(text))
    else:
        # Hugging Face fast tokenizers expose original character offsets.
        if not callable(tokenizer):
            raise TypeError("tokenizer must provide token_offsets or fast-tokenizer offset_mapping")
        result = tokenizer(text, add_special_tokens=False, return_offsets_mapping=True)
        spans = [tuple(span) for span in result["offset_mapping"] if span[1] > span[0]]
    if any(not (0 <= start < end <= len(text)) for start, end in spans):
        raise ValueError("tokenizer returned invalid character offsets")
    if any(spans[index][0] > spans[index + 1][0] for index in range(len(spans) - 1)):
        raise ValueError("tokenizer offsets are not ordered")
    return spans


def chunk_pages(
    pages: Sequence[DocumentPage],
    *,
    tokenizer: Tokenizer | None = None,
    max_tokens: int = 400,
    overlap_tokens: int = 60,
    count_tokens: Sequence[Callable[[str], int]] = (),
) -> tuple[Chunk, ...]:
    """Split each page separately, preserving exact characters and provenance.

    ``count_tokens`` adds guards for other pinned model tokenizers.  Every
    emitted substring must fit all guards; overlap is measured by the primary
    tokenizer.  Input whitespace is kept inside quotes, never normalized.
    """
    if type(max_tokens) is not int or max_tokens <= 0:
        raise ValueError("max_tokens must be a positive integer")
    if type(overlap_tokens) is not int or not 0 <= overlap_tokens < max_tokens:
        raise ValueError("overlap_tokens must be nonnegative and smaller than max_tokens")
    tokenizer = tokenizer or TiktokenTokenizer()
    chunks: list[Chunk] = []
    for page in pages:
        if type(page.page_number) is not int or page.page_number <= 0 or type(page.char_offset) is not int or page.char_offset < 0:
            raise ValueError("page number and original character offset are invalid")
        if not page.text.strip():
            continue
        spans = _offsets(tokenizer, page.text)
        if not spans:
            raise ValueError("nonempty page produced no tokens")
        start = 0
        while start < len(page.text):
            first_token = next((i for i, (_, end) in enumerate(spans) if end > start), len(spans))
            if first_token == len(spans):
                break
            last_token = min(first_token + max_tokens, len(spans))
            end = spans[last_token - 1][1]
            if last_token == len(spans):
                end = len(page.text)
            # Shared Evidence.quote allows at most 20,000 characters.  An
            # unusually whitespace-heavy 400-token page can exceed that.
            end = min(end, start + 20_000)
            while end > start:
                piece = page.text[start:end]
                token_count = _count(tokenizer, piece)
                if token_count <= max_tokens and all(count(piece) <= max_tokens for count in count_tokens):
                    break
                # Reducing one token boundary usually resolves split-character
                # retokenization; reducing a character handles a single huge
                # token under a second model's tokenizer as well.
                previous = [span_end for _, span_end in spans[first_token:last_token] if start < span_end < end]
                end = previous[-1] if previous else end - 1
                last_token = next((i + 1 for i, (_, span_end) in reversed(list(enumerate(spans))) if span_end <= end), first_token)
            if end <= start:
                raise ValueError("one character exceeds a configured tokenizer limit")
            piece = page.text[start:end]
            if piece.strip():
                identity = f"{page.original_sha256}:{page.document_id}:{page.page_number}:{start}:{end}"
                chunks.append(Chunk(
                    chunk_id="chunk_" + hashlib.sha256(identity.encode()).hexdigest(),
                    document_id=page.document_id, source_id=page.source_id,
                    company_ids=page.company_ids, domain=page.domain,
                    publisher_group=page.publisher_group, page_number=page.page_number,
                    text=piece, char_start=page.char_offset + start, char_end=page.char_offset + end,
                    token_count=_count(tokenizer, piece), original_sha256=page.original_sha256,
                    published_at=page.published_at, event_date=page.event_date, language=page.language,
                ))
            if end == len(page.text):
                break
            consumed = [i for i, (span_start, span_end) in enumerate(spans) if span_start >= start and span_end <= end]
            next_start = spans[consumed[-overlap_tokens]][0] if overlap_tokens and len(consumed) > overlap_tokens else end
            # Identical offsets can occur for UTF-8 fragments, and small extra
            # tokenizer guards can consume fewer tokens than the overlap.
            start = next_start if next_start > start else end
    return tuple(chunks)
