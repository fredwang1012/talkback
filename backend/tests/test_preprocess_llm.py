"""Tests for segment.py and summaries.py using fake LLM calls. No network, no API key, no credits."""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "preprocess"))
from chunk_tts import chunk_paragraph  # noqa: E402
from segment import SegItem, SegResult, build_user, narrator_only, needs_llm, segment_paragraph, validate_segments  # noqa: E402
from summaries import SummaryResult, build_summaries, prefix_text, summary_points, trim_words  # noqa: E402

from app.schema import Book, Character, Narrator, Paragraph, Section  # noqa: E402

TEXT = '"Well?" said Holmes. "I see nothing," I answered.'
SPEAKERS = {"holmes", "watson", "narrator"}


def item(speaker, text, emotion=None):
    return SegItem(speaker=speaker, text=text, emotion=emotion)


GOOD = [item("holmes", '"Well?"', "contemplation"), item("narrator", "said Holmes."),
        item("watson", '"I see nothing,"', "helplessness"), item("narrator", "I answered.")]


def make_book(kind="novel", n=25, cast=True) -> Book:
    paras = [Paragraph(idx=i, section_id="s1" if i < 12 else "s2", text=f'Paragraph {i} has "quoted words" and futuretoken{i}.')
             for i in range(n)]
    return Book(
        id="b", title="T", author="A", kind=kind, license="x",
        sections=[Section(id="s1", title="I.", start_para=0), Section(id="s2", title="II.", start_para=12)],
        paragraphs=paras,
        characters=[Character(id="holmes", name="Sherlock Holmes", aliases=["Holmes"], first_para=0, voice="oliver", persona="x"),
                    Character(id="watson", name="Dr. Watson", aliases=["Watson"], first_para=0, voice="marcus", persona="y")] if cast else [],
        narrator=Narrator(voice="marcus", is_character="watson") if cast else None,
    )


def run(coro):
    return asyncio.run(coro)


# ---------- segment validation ----------

def test_valid_tiling_is_accepted_and_keeps_emotions():
    segs = validate_segments(TEXT, GOOD, SPEAKERS)
    assert [s.speaker for s in segs] == ["holmes", "narrator", "watson", "narrator"]
    assert segs[0].emotion == "contemplation" and segs[1].emotion is None


@pytest.mark.parametrize("name,items", [
    ("dropped text", GOOD[:2] + GOOD[3:]),
    ("reordered", [GOOD[1], GOOD[0], GOOD[2], GOOD[3]]),
    ("reworded", [item("holmes", '"Well, then?"'), *GOOD[1:]]),
    ("trailing text missing", GOOD[:3]),
    ("empty segment", [*GOOD, item("narrator", "   ")]),
    ("nothing", []),
])
def test_bad_tilings_are_rejected(name, items):
    assert validate_segments(TEXT, items, SPEAKERS) is None, name


def test_unknown_speaker_becomes_narrator_rather_than_failing():
    segs = validate_segments(TEXT, [item("the-king", '"Well?"'), *GOOD[1:]], SPEAKERS)
    assert segs[0].speaker == "narrator"


def test_whitespace_differences_are_tolerated():
    p = 'He said:\n  "Yes."'
    assert validate_segments(p, [item("narrator", "He said:"), item("holmes", '"Yes."')], SPEAKERS) is not None


def test_validated_segments_always_chunk_without_error():
    book = make_book()
    p = Paragraph(idx=0, section_id="s1", text=TEXT, segments=validate_segments(TEXT, GOOD, SPEAKERS))
    chunks = chunk_paragraph(p, book)
    assert [c.speaker for c in chunks] == ["holmes", "narrator", "watson", "narrator"]
    assert chunks[0].voice == "oliver" and "<|emotion:contemplation|>" in chunks[0].tts_text


# ---------- segment_paragraph orchestration ----------

class FakeCall:
    def __init__(self, *answers):
        self.answers, self.calls = list(answers), 0

    async def __call__(self, model, system, user, schema, **kw):
        self.calls += 1
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


class Refusal(Exception):
    pass


def para(book, text=TEXT):
    return Paragraph(idx=5, section_id="s1", text=text)


def test_paragraphs_without_quotes_skip_the_llm():
    book, call = make_book(), FakeCall()
    segs, how = run(segment_paragraph(book, para(book, "No dialogue here at all."), call))
    assert how == "skipped" and call.calls == 0 and segs == narrator_only("No dialogue here at all.")


def test_textbooks_and_uncast_novels_never_call_the_llm():
    assert not needs_llm(make_book("textbook"), para(make_book()))
    assert not needs_llm(make_book(cast=False), para(make_book()))
    assert needs_llm(make_book(), para(make_book()))


def test_good_answer_is_used_once():
    book, call = make_book(), FakeCall(SegResult(segments=GOOD))
    segs, how = run(segment_paragraph(book, para(book), call))
    assert how == "llm" and call.calls == 1 and len(segs) == 4


def test_bad_then_good_retries_once():
    book, call = make_book(), FakeCall(SegResult(segments=GOOD[:2]), SegResult(segments=GOOD))
    _, how = run(segment_paragraph(book, para(book), call))
    assert how == "retry" and call.calls == 2


def test_bad_twice_falls_back_to_narrator_only():
    book, call = make_book(), FakeCall(SegResult(segments=GOOD[:2]), SegResult(segments=GOOD[:1]))
    segs, how = run(segment_paragraph(book, para(book), call))
    assert how == "fallback" and call.calls == 2
    assert [s.speaker for s in segs] == ["narrator"] and segs[0].text == TEXT


def test_refusal_falls_back_without_retrying():
    book, call = make_book(), FakeCall(Refusal("no"))
    segs, how = run(segment_paragraph(book, para(book), call, Refusal))
    assert how == "fallback" and call.calls == 1 and segs[0].speaker == "narrator"


def test_prompt_contains_cast_and_only_previous_paragraphs():
    book = make_book()
    p = book.paragraphs[10]
    user = build_user(book, p)
    assert 'id "holmes"' in user and "first-person narrator" in user
    assert "futuretoken9" in user and "futuretoken7" in user and "futuretoken6" not in user  # exactly 3 before
    assert "futuretoken11" not in user  # never later text


# ---------- summaries ----------

def test_summary_points_cover_every_ten_section_ends_and_the_last_paragraph():
    book = make_book(n=25)
    pts = summary_points(book)
    assert pts == sorted(set(pts)) and 9 in pts and 19 in pts
    assert 11 in pts, "end of section I (the paragraph before section II starts)"
    assert pts[-1] == 24


def test_prefix_text_never_contains_later_paragraphs():
    book = make_book()
    for upto in summary_points(book):
        text = prefix_text(book, upto)
        assert f"futuretoken{upto}." in text
        assert not any(f"futuretoken{j}." in text for j in range(upto + 1, len(book.paragraphs)))


def test_summaries_are_built_from_prefix_only():
    book = make_book()
    seen = []

    async def fake(model, system, user, schema, **kw):
        upto = max(int(t.split("futuretoken")[1].split(".")[0]) for t in user.split() if "futuretoken" in t)
        seen.append((upto, user))
        return SummaryResult(summary=f"covers up to {upto}")

    out = run(build_summaries(book, fake))
    assert [s.upto_para for s in out] == summary_points(book)
    for upto, user in seen:
        assert not any(f"futuretoken{j}." in user for j in range(upto + 1, len(book.paragraphs)))
    assert all(s.text == f"covers up to {s.upto_para}" for s in out)


def test_trim_words_caps_runaway_summaries():
    assert trim_words("short note.") == "short note."
    long = " ".join(["word"] * 400)
    assert len(trim_words(long).split()) <= 150


# ---------- textbook mode ----------

from cast import TUTOR_VOICE, cast_textbook  # noqa: E402
from segment import Pick, PickResult, apply_emphasis, emphasize_textbook  # noqa: E402


def test_cast_textbook_has_no_characters_and_a_tutor_voice():
    book = make_book("textbook")
    cast_textbook(book)
    assert book.characters == [] and book.narrator.voice == TUTOR_VOICE and book.narrator.is_character is None


def test_apply_emphasis_tiles_the_paragraph():
    text = "Plants use light. It is astonishing how much energy arrives. Then sugar forms."
    segs = apply_emphasis(text, "It is astonishing how much energy arrives.", "awe")
    assert [s.emotion for s in segs] == [None, "awe", None]
    assert " ".join(s.text for s in segs) == text
    assert apply_emphasis(text, "not in the text", "awe") is None


def test_emphasize_textbook_only_applies_verbatim_picks_and_defaults_to_narrator():
    book = make_book("textbook", n=10, cast=False)
    for p in book.paragraphs:
        p.text = f"Plain fact {p.idx}. Striking claim {p.idx}!"
    seen = []

    async def fake(model, system, user, schema, **kw):
        seen.append(user)
        return PickResult(picks=[Pick(para=3, sentence="Striking claim 3!", emotion="awe"),
                                 Pick(para=4, sentence="invented sentence", emotion="awe")])

    n = run(emphasize_textbook(book, fake))
    assert n == 1
    assert [s.emotion for s in book.paragraphs[3].segments] == [None, "awe"]
    assert all(len(p.segments) >= 1 and p.segments[0].speaker == "narrator" for p in book.paragraphs)
    assert len(seen) == 1  # one call per non-empty section (section 2 has no paragraphs)
