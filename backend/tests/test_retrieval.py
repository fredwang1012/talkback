import zlib
from pathlib import Path

import numpy as np
import pytest
from hypothesis import given, settings as hsettings, strategies as st

from app.retrieval import (
    CHUNK_MAX, CHUNK_MIN, CHUNK_OVERLAP_CHARS, CHUNK_TARGET, DEFAULT_K, MAX_PASSAGE_CHARS, REALTIME_K,
    REALTIME_MAX_PASSAGE_CHARS, Index, latest_summary, make_chunks, recent_passage, retrieve,
)
from app.schema import Book, Paragraph, Section, Summary

DIM = 64
ROOT = Path(__file__).resolve().parents[2]


class FakeEmbedder:
    """Deterministic bag-of-words hashing embedder so hybrid mode can be tested without a model."""
    def embed(self, text: str) -> np.ndarray:
        v = np.zeros(DIM)
        for w in text.lower().split():
            v[zlib.crc32(w.encode()) % DIM] += 1
        n = np.linalg.norm(v)
        return v / n if n else v

    embed_query = embed


def make_book(n=60, kind="novel", summaries=True) -> Book:
    paras = [Paragraph(idx=i, section_id="s1", text=f"Paragraph {i} mentions secretword{i} and the common story text.")
             for i in range(n)]
    sums = [Summary(upto_para=u, text=f"summary up to {u}") for u in (9, 19, 29, 39, 49)] if summaries else []
    return Book(id="b", title="T", author="A", kind=kind, license="x",
                sections=[Section(id="s1", title="I.", start_para=0)], paragraphs=paras, summaries=sums)


def hybrid_index(book: Book) -> Index:
    chunks = small_chunks(book)
    emb = np.stack([FakeEmbedder().embed(c.text) for c in chunks])
    return Index(chunks, emb, FakeEmbedder())


def small_chunks(book):
    """Chunks sized for the ~55-char synthetic paragraphs used here (about 3 paragraphs each, 1 shared)."""
    return make_chunks(book, target=160, min_chars=100, max_chars=220, overlap_chars=60)


def book_of(lengths):
    paras = [Paragraph(idx=i, section_id="s1", text="x" * n) for i, n in enumerate(lengths)]
    return Book(id="b", title="T", author="A", kind="novel", license="x",
                sections=[Section(id="s1", title="I.", start_para=0)], paragraphs=paras)


def test_make_chunks_overlap_and_coverage():
    chunks = small_chunks(make_book(10))
    assert chunks[0].para_start == 0 and chunks[0].para_end >= 1
    assert chunks[1].para_start == chunks[0].para_end  # one paragraph of overlap
    assert chunks[-1].para_end == 9
    covered = {i for c in chunks for i in range(c.para_start, c.para_end + 1)}
    assert covered == set(range(10))


def test_default_chunks_target_about_1400_chars():
    book = book_of([60] * 200)  # short dialogue-like paragraphs
    chunks = make_chunks(book)
    sizes = [len(c.text) for c in chunks[:-1]]
    assert all(CHUNK_MIN <= n <= CHUNK_MAX for n in sizes)
    assert 1000 <= sum(sizes) / len(sizes) <= 1500


def test_single_huge_paragraph_is_its_own_chunk():
    chunks = make_chunks(book_of([100, 5000, 100]))
    huge = [c for c in chunks if c.para_start <= 1 <= c.para_end and len(c.text) > CHUNK_MAX]
    assert huge and all(c.para_start == c.para_end == 1 for c in huge)


@given(lengths=st.lists(st.integers(1, 3000), min_size=1, max_size=120))
@hsettings(max_examples=200, deadline=None)
def test_chunker_properties(lengths):
    book = book_of(lengths)
    chunks = make_chunks(book)
    assert chunks[0].para_start == 0 and chunks[-1].para_end == len(lengths) - 1
    assert len({c.id for c in chunks}) == len(chunks)
    covered = set()
    for prev, c in zip([None] + chunks, chunks):
        covered.update(range(c.para_start, c.para_end + 1))
        assert c.text == "\n".join(p.text for p in book.paragraphs[c.para_start:c.para_end + 1])
        if c.para_start != c.para_end:
            assert len(c.text) <= CHUNK_MAX  # only a lone oversized paragraph may exceed the maximum
        if prev is not None:
            assert c.para_start > prev.para_start and c.para_end > prev.para_end  # ordered, always advancing
            assert c.para_start <= prev.para_end + 1  # no gaps
            shared = book.paragraphs[c.para_start:prev.para_end + 1]
            if shared and len(shared) > 1:
                assert sum(len(p.text) + 1 for p in shared) <= CHUNK_OVERLAP_CHARS
    assert covered == set(range(len(lengths)))
    for c in chunks[:-1]:  # all but the last reach the minimum unless the next paragraph would blow the maximum
        nxt = lengths[c.para_end + 1] + 1 if c.para_end + 1 < len(lengths) else 0
        assert len(c.text) + 1 >= CHUNK_MIN or len(c.text) + 1 + nxt > CHUNK_MAX


def test_neighbouring_chunks_overlap_by_a_paragraph():
    chunks = make_chunks(book_of([100] * 60))
    assert all(b.para_start <= a.para_end for a, b in zip(chunks, chunks[1:]))


def test_caps_and_defaults_are_consistent():
    assert DEFAULT_K == 8 and MAX_PASSAGE_CHARS == 12000
    assert REALTIME_K < DEFAULT_K and REALTIME_MAX_PASSAGE_CHARS < MAX_PASSAGE_CHARS


def test_retrieve_respects_max_chars():
    book = book_of([1500] * 40)
    out = retrieve(book, Index(make_chunks(book)), "x", reader_para=39, k=8, max_chars=REALTIME_MAX_PASSAGE_CHARS)
    assert sum(len(p["text"]) for p in out["passages"]) <= REALTIME_MAX_PASSAGE_CHARS
    out = retrieve(book, Index(make_chunks(book)), "x", reader_para=39)
    assert sum(len(p["text"]) for p in out["passages"]) <= MAX_PASSAGE_CHARS


@given(reader=st.integers(0, 59), q=st.sampled_from(["how does it end", "secretword55 mystery", "the common story", ""]),
       k=st.integers(1, 8), hybrid=st.booleans())
@hsettings(max_examples=150, deadline=None)
def test_spoiler_invariant_property(reader, q, k, hybrid):
    book = make_book()
    index = hybrid_index(book) if hybrid else Index(small_chunks(book))
    out = retrieve(book, index, q, reader, k)
    assert out["passages"], "there is always at least the 'what just happened' passage"
    assert all(p["para_end"] <= reader for p in out["passages"])
    assert not any(f"secretword{j}" in p["text"] for p in out["passages"] for j in range(reader + 1, 60))
    if out["story_so_far"]:
        upto = int(out["story_so_far"].split()[-1])
        assert upto <= reader


def test_future_text_never_returned_even_when_query_matches_it_exactly():
    book = make_book()
    out = retrieve(book, hybrid_index(book), "secretword50", reader_para=10)
    assert all("secretword50" not in p["text"] for p in out["passages"])
    out = retrieve(book, hybrid_index(book), "secretword5", reader_para=10)
    assert any("secretword5 " in p["text"] for p in out["passages"])  # earlier text is found


def test_recent_paragraphs_always_included():
    book = make_book()
    out = retrieve(book, Index(small_chunks(book)), "secretword3", reader_para=40)
    text = " ".join(p["text"] for p in out["passages"])
    assert "secretword40" in text and "secretword39" in text


def test_summary_is_largest_not_beyond_position():
    book = make_book()
    assert latest_summary(book, 5) == ""
    assert latest_summary(book, 9) == "summary up to 9"
    assert latest_summary(book, 38) == "summary up to 29"
    assert latest_summary(book, 59) == "summary up to 49"


def test_passage_cap_holds_with_huge_paragraphs():
    paras = [Paragraph(idx=i, section_id="s1", text="word " * 3000) for i in range(20)]
    book = Book(id="b", title="T", author="A", kind="novel", license="x",
                sections=[Section(id="s1", title="I.", start_para=0)], paragraphs=paras)
    out = retrieve(book, Index(small_chunks(book)), "word", reader_para=19, k=6)
    assert sum(len(p["text"]) for p in out["passages"]) <= MAX_PASSAGE_CHARS
    assert out["passages"]


def test_reader_para_is_clamped():
    book = make_book(10)
    assert retrieve(book, Index(small_chunks(book)), "x", reader_para=999)["reader_para"] == 9
    assert retrieve(book, Index(small_chunks(book)), "x", reader_para=-5)["reader_para"] == 0


def test_textbook_is_not_spoiler_filtered_but_prefers_nearby():
    book = make_book(60, kind="textbook", summaries=False)
    index = Index(small_chunks(book))
    out = retrieve(book, index, "the common story", reader_para=10, k=4)
    assert out["story_so_far"] == ""
    assert any(p["para_end"] > 10 for p in out["passages"]), "tutor may use the whole chapter"
    near = index.search("zzz unmatched", 10, k=2, spoiler_safe=False)
    assert all(abs(10 - (c.para_start + c.para_end) / 2) <= 15 for c in near)


def test_bm25_only_mode_when_no_embeddings():
    book = make_book()
    assert Index(small_chunks(book)).mode == "bm25"
    assert hybrid_index(book).mode == "hybrid"


def test_recent_passage_bounds():
    book = make_book(10)
    assert recent_passage(book, 0)["para_start"] == 0
    assert recent_passage(book, 9) == {"para_start": 8, "para_end": 9, "text": book.paragraphs[8].text + "\n" + book.paragraphs[9].text}


@given(reader=st.integers(0, 258), q=st.sampled_from(["Who is the stranger?", "How does the story end?", "secretword7 lantern"]))
@hsettings(max_examples=60, deadline=None)
def test_spoiler_invariant_on_long_synthetic_book(reader, q):
    book = make_book(259)
    out = retrieve(book, Index(small_chunks(book)), q, reader)
    assert all(p["para_end"] <= reader for p in out["passages"])
