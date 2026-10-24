"""rechunk_book scans the sections once per book, not once per paragraph (review of #133), with identical output."""
import json
from pathlib import Path

import pytest

from app.schema import Book, Paragraph, Section, Segment
from app.ttschunks import chunk_paragraph, lead_pause, rechunk_book, sing_offset

BOOKS = Path(__file__).resolve().parents[2] / "data" / "books"


class CountingList(list):
    iterations = 0

    def __iter__(self):
        CountingList.iterations += 1
        return super().__iter__()


def big_book(n: int = 400, every: int = 4) -> Book:
    paras = [Paragraph(idx=i, section_id=f"s{i // every}", text=f"Line {i} of the story.",
                       segments=[Segment(speaker="narrator", text=f"Line {i} of the story.")]) for i in range(n)]
    sections = [Section(id=f"s{k}", title=f"Chapter {k}", start_para=k * every) for k in range(n // every)]
    return Book(id="big", title="T", author="A", kind="novel", license="x", sections=sections, paragraphs=paras)


def test_rechunk_scans_sections_a_constant_number_of_times():
    book = big_book()
    book.sections = CountingList(book.sections)
    CountingList.iterations = 0
    rechunk_book(book)
    assert CountingList.iterations <= 3, CountingList.iterations
    assert book.paragraphs[4].tts_chunks[0].tts_text.startswith("<|prosody:long_pause|>")


def test_single_paragraph_calls_still_work_without_an_index():
    book = big_book(40)
    assert lead_pause(book.paragraphs[8], book) == "long_pause" and lead_pause(book.paragraphs[9], book) is None
    assert sing_offset(book.paragraphs[9], book) is None
    assert chunk_paragraph(book.paragraphs[8], book)[0].tts_text.startswith("<|prosody:long_pause|>")


def test_a_changed_section_list_is_seen_by_single_paragraph_calls():
    book = big_book(40)
    assert lead_pause(book.paragraphs[9], book) is None
    book.sections = book.sections + [Section(id="x", title="Extra", start_para=9)]
    assert lead_pause(book.paragraphs[9], book) == "long_pause"


@pytest.mark.parametrize("path", sorted(BOOKS.glob("*/book.json")), ids=lambda p: p.parent.name)
def test_committed_books_rechunk_to_their_stored_chunks(path):
    book = Book.model_validate_json(path.read_text(encoding="utf-8"))
    stored = json.dumps([[c.model_dump() for c in p.tts_chunks] for p in book.paragraphs])
    rechunk_book(book)
    assert json.dumps([[c.model_dump() for c in p.tts_chunks] for p in book.paragraphs]) == stored
