from dataclasses import replace

import pytest

from main.agents.supervisor.contracts import Domain
from main.rag.neutral.chunking import DocumentPage, TiktokenTokenizer, chunk_pages


class CharacterTokenizer:
    def encode(self, text):
        return list(range(len(text)))

    def token_offsets(self, text):
        return [(index, index + 1) for index in range(len(text))]


def page(text, *, number=1):
    return DocumentPage("document", "source", ("14",), Domain.PHYSICAL_AI_ROBOTICS,
                        "company_14", number, text, "0" * 64)


def test_default_400_tokens_and_60_token_overlap_are_measured_by_tokenizer():
    original = page("x" * 800)
    chunks = chunk_pages([original], tokenizer=CharacterTokenizer())
    assert [(chunk.char_start, chunk.char_end) for chunk in chunks] == [(0, 400), (340, 740), (680, 800)]
    assert [chunk.token_count for chunk in chunks] == [400, 400, 120]
    assert all(original.text[chunk.char_start:chunk.char_end] == chunk.text for chunk in chunks)


def test_korean_utf8_fragments_never_change_quote_or_exceed_tokens():
    original = page("한국어 기술 구조와 한계, 검증 조건.\n\t" * 200)
    tokenizer = TiktokenTokenizer()
    chunks = chunk_pages([original], tokenizer=tokenizer)
    assert len(chunks) > 1
    assert all(chunk.text == original.text[chunk.char_start:chunk.char_end] for chunk in chunks)
    assert all(chunk.token_count == len(tokenizer.encode(chunk.text)) <= 400 for chunk in chunks)
    assert all("�" not in chunk.text for chunk in chunks)
    assert chunks[0].char_start == 0
    assert chunks[-1].char_end == len(original.text)


def test_page_boundaries_and_nullable_provenance_survive():
    pages = [page("a" * 450), page("b" * 420, number=2)]
    chunks = chunk_pages(pages, tokenizer=CharacterTokenizer())
    assert {chunk.page_number for chunk in chunks} == {1, 2}
    for chunk in chunks:
        original = pages[chunk.page_number - 1]
        assert chunk.text == original.text[chunk.char_start:chunk.char_end]
        assert chunk.company_ids == ("14",)
        assert chunk.publisher_group == "company_14"
        assert chunk.published_at is None
        assert chunk.event_date is None


def test_additional_candidate_tokenizer_limit_preserves_real_substrings():
    original = page("abcde" * 200)
    chunks = chunk_pages([original], tokenizer=CharacterTokenizer(), count_tokens=[lambda text: len(text) * 2])
    assert all(len(chunk.text) * 2 <= 400 for chunk in chunks)
    assert chunks[-1].char_end == len(original.text)
    assert all(original.text[chunk.char_start:chunk.char_end] == chunk.text for chunk in chunks)


def test_scoped_excerpt_uses_absolute_original_page_offsets():
    scoped = replace(page("company quote text"), char_offset=57)
    chunks = chunk_pages([scoped], tokenizer=CharacterTokenizer())
    assert (chunks[0].char_start, chunks[0].char_end) == (57, 75)


def test_chunk_ids_are_stable_and_blank_pages_produce_no_evidence():
    original = page("stable quotation")
    assert chunk_pages([original]) == chunk_pages([original])
    assert chunk_pages([page(" \n\t")]) == ()


def test_whitespace_heavy_token_window_also_fits_shared_evidence_character_limit():
    original = page(" " * 50000 + "verified statement")
    chunks = chunk_pages([original])
    assert chunks
    assert all(len(chunk.text) <= 20000 for chunk in chunks)
    assert all(original.text[chunk.char_start:chunk.char_end] == chunk.text for chunk in chunks)


@pytest.mark.parametrize("size,overlap", [(0, 0), (400, 400), (400, -1), (True, 0)])
def test_invalid_chunk_policy_is_rejected(size, overlap):
    with pytest.raises(ValueError):
        chunk_pages([page("text")], max_tokens=size, overlap_tokens=overlap)
