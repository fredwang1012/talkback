"""Tests for segment.py and summaries.py using fake LLM calls. No network, no API key, no credits."""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "preprocess"))
from chunk_tts import chunk_paragraph  # noqa: E402
from segment import SegItem, SegResult, build_user, narrator_only, needs_llm, segment_paragraph, validate_segments  # noqa: E402
from summaries import MAX_WORDS, SummaryResult, build_summaries, prefix_text, summary_points, trim_words  # noqa: E402

from app.schema import Book, Character, Narrator, Paragraph, Section  # noqa: E402

TEXT = '"Well?" said Harry. "I see nothing," I answered.'
SPEAKERS = {"harry", "pell", "narrator"}


def item(speaker, text, emotion=None):
    return SegItem(speaker=speaker, text=text, emotion=emotion)


GOOD = [item("harry", '"Well?"', "contemplation"), item("narrator", "said Harry."),
        item("pell", '"I see nothing,"', "helplessness"), item("narrator", "I answered.")]


def make_book(kind="novel", n=25, cast=True) -> Book:
    paras = [Paragraph(idx=i, section_id="s1" if i < 12 else "s2", text=f'Paragraph {i} has "quoted words" and futuretoken{i}.')
             for i in range(n)]
    return Book(
        id="b", title="T", author="A", kind=kind, license="x",
        sections=[Section(id="s1", title="I.", start_para=0), Section(id="s2", title="II.", start_para=12)],
        paragraphs=paras,
        characters=[Character(id="harry", name="Harry Quill", aliases=["Harry"], first_para=0, voice="oliver", persona="x"),
                    Character(id="pell", name="Dr. Pell", aliases=["Pell"], first_para=0, voice="marcus", persona="y")] if cast else [],
        narrator=Narrator(voice="marcus", is_character="pell") if cast else None,
    )


def run(coro):
    return asyncio.run(coro)


# ---------- segment validation ----------

def test_valid_tiling_is_accepted_and_keeps_emotions():
    segs = validate_segments(TEXT, GOOD, SPEAKERS)
    assert [s.speaker for s in segs] == ["harry", "narrator", "pell", "narrator"]
    assert segs[0].emotion == "contemplation" and segs[1].emotion is None


@pytest.mark.parametrize("name,items", [
    ("dropped text", GOOD[:2] + GOOD[3:]),
    ("reordered", [GOOD[1], GOOD[0], GOOD[2], GOOD[3]]),
    ("reworded", [item("harry", '"Well, then?"'), *GOOD[1:]]),
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
    assert validate_segments(p, [item("narrator", "He said:"), item("harry", '"Yes."')], SPEAKERS) is not None


def test_validated_segments_always_chunk_without_error():
    book = make_book()
    p = Paragraph(idx=0, section_id="s1", text=TEXT, segments=validate_segments(TEXT, GOOD, SPEAKERS))
    chunks = chunk_paragraph(p, book)
    assert [c.speaker for c in chunks] == ["harry", "narrator", "pell", "narrator"]
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
    assert 'id "harry"' in user and "first-person narrator" in user
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
    assert len(trim_words(long).split()) <= MAX_WORDS + 30


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


# ---------- incremental summaries, batched segmentation, series casting ----------

def test_incremental_summaries_see_previous_note_and_only_the_new_window():
    book = make_book(n=25)
    calls = []

    async def fake(model, system, user, schema, **kw):
        calls.append(user)
        return SummaryResult(summary=f"note {len(calls)}")

    out = run(build_summaries(book, fake))
    pts = summary_points(book)
    assert [s.upto_para for s in out] == pts
    for i, (upto, user) in enumerate(zip(pts, calls)):
        start = pts[i - 1] + 1 if i else 0
        assert f"futuretoken{upto}." in user and f"futuretoken{start}." in user
        assert not any(f"futuretoken{j}." in user for j in range(upto + 1, 25)), "never later text"
        assert not any(f"futuretoken{j}." in user for j in range(0, start)), "earlier text arrives via the note"
        assert (f"note {i}" in user) if i else "(the story has just begun)" in user


def test_long_books_use_sparser_summary_points():
    from summaries import LONG_BOOK, LONG_EVERY
    book = make_book(n=LONG_BOOK + 100)
    pts = summary_points(book)
    assert LONG_EVERY - 1 in pts and 9 not in pts


from segment import BatchPara, BatchResult, batches, build_batch_user, segment_batch  # noqa: E402


def quote_book():
    texts = ['"Hi," said Harry.', 'Plain narration.', '"Yes," said Pell. "No."', '"Bye," said Harry.']
    book = make_book(n=4)
    for p, t in zip(book.paragraphs, texts):
        p.text = t
    return book


def test_batches_only_include_quoted_paragraphs():
    book = quote_book()
    assert [[p.idx for p in b] for b in batches(book)] == [[0, 2, 3]]


def test_batch_prompt_has_cast_numbered_paragraphs_and_no_later_text():
    book = quote_book()
    user = build_batch_user(book, [book.paragraphs[2]])
    assert 'id "harry"' in user and '[2] "Yes," said Pell.' in user
    assert "Bye" not in user and "Plain narration." in user  # previous paragraphs as context only


def test_segment_batch_validates_each_paragraph_and_retries_failures_alone():
    book = quote_book()
    paras = [book.paragraphs[0], book.paragraphs[2]]
    single_calls = []

    async def fake(model, system, user, schema, **kw):
        if schema is BatchResult:
            return BatchResult(paragraphs=[
                BatchPara(para=0, segments=[item("harry", '"Hi,"'), item("narrator", "said Harry.")]),
                BatchPara(para=2, segments=[item("pell", '"Yes,"')])])  # does not tile: retried alone
        single_calls.append(user)
        return SegResult(segments=[item("pell", '"Yes,"'), item("narrator", "said Pell."), item("pell", '"No."')])

    out = run(segment_batch(book, paras, fake))
    assert [h for _, h in out] == ["llm", "llm"]
    assert [s.speaker for s in out[1][0]] == ["pell", "narrator", "pell"]
    assert len(single_calls) == 1


from cast import CastCharacter, CastResult, apply_series_cast, speaker_stats  # noqa: E402


def series(i, texts):
    b = make_book(n=len(texts), cast=False)
    b.id, b.title, b.series, b.series_index = f"b{i}", f"Book {i}", "saga", i
    b.sections = [Section(id="s1", title="I.", start_para=0)]
    for p, t in zip(b.paragraphs, texts):
        p.text, p.section_id = t, "s1"
    return b


def test_speaker_stats_counts_tags_and_keeps_earliest_lines():
    b1 = series(1, ['"Hello," said Harry.', '"Hm," said Hagrid. "Yes," Harry said.', '"Again," said Harry.'])
    stats = {s["name"]: s for s in speaker_stats([b1])}
    assert stats["Harry"]["count"] == 3 and stats["Hagrid"]["count"] == 1
    assert stats["Harry"]["first"][:2] == (1, 0) and stats["Harry"]["lines"][0] == '"Hello," said Harry.'
    assert "He" not in stats


def test_series_cast_gates_characters_by_first_appearance_across_books():
    b1 = series(1, ["Harry woke.", "Then Hagrid came."])
    b2 = series(2, ["Dobby appeared.", "Harry sighed."])
    b3 = series(3, ["Nothing new here."])

    def ch(id_, name):
        return CastCharacter(id=id_, name=name, aliases=[], first_para=0, gender="male", persona="p",
                             voice="jake", voice_tags="")

    result = CastResult(characters=[ch("harry", "Harry"), ch("hagrid", "Hagrid"), ch("dobby", "Dobby"),
                                    ch("lupin", "Lupin")], narrator_voice="nora", narrator_is_character=None)
    apply_series_cast([b1, b2, b3], result)
    assert {c.id: c.first_para for c in b1.characters} == {"harry": 0, "hagrid": 1}
    assert {c.id: c.first_para for c in b2.characters} == {"harry": 0, "hagrid": 0, "dobby": 0}
    assert {c.id for c in b3.characters} == {"harry", "hagrid", "dobby"}  # met earlier: known; lupin never appears
