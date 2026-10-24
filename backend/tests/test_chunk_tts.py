import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "preprocess"))
from chunk_tts import (  # noqa: E402
    MAX_CHARS, build_tts_text, chunk_paragraph, normalize_for_tts, sentence_spans, split_long,
)

from app.schema import Book, Character, Narrator, Paragraph, Section, Segment  # noqa: E402


def sents(text):
    return [text[s:e] for s, e in sentence_spans(text)]


def test_abbreviations_and_initials_do_not_split():
    assert sents("Mr. Harry met Dr. Pell. They left.") == ["Mr. Harry met Dr. Pell.", "They left."]
    assert sents("J. Smith arrived. Then he left.") == ["J. Smith arrived.", "Then he left."]
    assert sents("St. Monica's church stood near. It was old.") == ["St. Monica's church stood near.", "It was old."]


def test_quotes_questions_and_ellipses():
    assert sents('"Well?" said he. "Quite so."') == ['"Well?" said he.', '"Quite so."']
    assert sents("He paused... and then went on. Done.") == ["He paused... and then went on.", "Done."]
    assert sents("What? No! Yes.") == ["What?", "No!", "Yes."]


def test_split_long_prefers_commas():
    text = ("word " * 30).strip() + ", " + ("more " * 40).strip() + ", end"
    parts = [text[s:e] for s, e in split_long(text, 0)]
    assert all(len(p) <= MAX_CHARS for p in parts)
    assert parts[0].endswith(",")
    assert " ".join(parts) == text


def test_normalize_numbers():
    assert normalize_for_tts("on the twentieth of March, 1888,") == "on the twentieth of March, eighteen eighty-eight,"
    assert normalize_for_tts("in 1905 and 2003") == "in nineteen oh five and two thousand three"
    assert normalize_for_tts("a thousand pounds, 1,000 of them, 3 times") == "a thousand pounds, one thousand of them, three times"
    assert normalize_for_tts("Mr. Harry") == "Mr. Harry"


def test_tag_placement():
    assert build_tts_text("Hi.", "<|prosody:pitch_low|>", "anger") == "<|prosody:pitch_low|><|emotion:anger|>Hi."
    assert build_tts_text("Hi.", "", None) == "Hi."


def make_book(chars=()):
    return Book(id="b", title="T", author="A", kind="novel", license="x", sections=[], paragraphs=[],
                characters=list(chars), narrator=Narrator(voice="marcus"))


def covers(p: Paragraph, chunks):
    joined = " ".join(p.text[c.char_start:c.char_end] for c in chunks)
    return re.sub(r"\s+", " ", joined) == re.sub(r"\s+", " ", p.text)


LONG = " ".join(f"This is sentence number {i} of a rather long paragraph." for i in range(30))


@pytest.mark.parametrize("text", [
    "Short one.",
    LONG,
    "One " + "very " * 100 + "long sentence without any stop",
    '"Well, Pell, what do you make of it?" said he. "I cannot say," I answered. Mr. Harry laughed.',
])
def test_chunks_bounded_verbatim_nonempty(text):
    p = Paragraph(idx=3, section_id="s1", text=text)
    chunks = chunk_paragraph(p, make_book())
    assert chunks and all(c.char_end > c.char_start for c in chunks)
    assert all(len(p.text[c.char_start:c.char_end]) <= MAX_CHARS for c in chunks)
    assert covers(p, chunks)
    assert [c.id for c in chunks] == [f"p3c{i}" for i in range(len(chunks))]
    assert all(c.voice == "marcus" and c.speaker == "narrator" for c in chunks)


def test_segments_voice_tags_and_emotion():
    text = '"I see." said Harry. Then he smiled.'
    p = Paragraph(idx=0, section_id="s1", text=text, segments=[
        Segment(speaker="harry", text='"I see."', emotion="contemplation"),
        Segment(speaker="narrator", text="said Harry. Then he smiled."),
    ])
    harry = Character(id="harry", name="Harry Quill", first_para=0, voice="oliver",
                       voice_tags="<|prosody:pitch_low|>", persona="x")
    chunks = chunk_paragraph(p, make_book([harry]))
    assert chunks[0].voice == "oliver" and chunks[0].speaker == "harry"
    assert chunks[0].tts_text == '<|prosody:pitch_low|><|emotion:contemplation|>"I see."'
    assert chunks[1].voice == "marcus" and chunks[1].tts_text.startswith("said Harry.")
    assert covers(p, chunks)
    # chunks never cross a segment boundary
    assert chunks[0].char_end <= chunks[1].char_start


def synthetic_long_book(n=60) -> Book:
    """A book of varied paragraphs (dialogue, long sentences, short lines) with no real-book dependency."""
    base = [
        '"Come along," said the keeper. "The lantern road is long, and we must reach the gate before the bell."',
        "The hill rolled down toward a grey harbour where small boats knocked together in the wind, and every "
        "window along the quay had a candle in it, and every candle trembled whenever the tide pushed a cold "
        "breath through the cracks in the old stone, so that the whole town seemed to be breathing slowly.",
        "Nobody answered.",
        "She counted the steps twice, then a third time; the number never changed, but her certainty did.",
    ]
    paras = [Paragraph(idx=i, section_id="s1", text=f"{base[i % len(base)]} Marker {i}.") for i in range(n)]
    return Book(id="syn", title="T", author="A", kind="novel", license="x",
                sections=[Section(id="s1", title="I.", start_para=0)], paragraphs=paras,
                narrator=Narrator(voice="marcus"))


def test_long_synthetic_book_chunks_cover_and_respect_max():
    book = synthetic_long_book()
    for p in book.paragraphs:
        chunks = chunk_paragraph(p, book)
        assert chunks and covers(p, chunks), p.idx
        assert all(len(p.text[c.char_start:c.char_end]) <= MAX_CHARS for c in chunks), p.idx


def test_prerender_estimate_scales_with_chars():
    import prerender_tts as pr
    book = synthetic_long_book(300)  # roughly a 45K-char story
    for p in book.paragraphs:
        p.tts_chunks = chunk_paragraph(p, book)
    work = pr.pending_work(book)
    total = sum(w[3] for w in work)
    assert work and 30_000 < total < 60_000
    assert pr.estimate_usd(work) == pytest.approx(total / 1000 * pr.RATE_PER_1K_CHARS)
