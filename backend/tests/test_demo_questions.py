"""Position picking and prompt building for preprocess/demo_questions.py. No network."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "preprocess"))
from demo_questions import DemoSet, SYSTEM, available_characters, build_user, pick_positions, render_markdown, validate  # noqa: E402

from app.schema import Book, Character, Paragraph, Section, Summary  # noqa: E402


def make_book(n=100):
    paras = [Paragraph(idx=i, text=f"para {i}", section_id=f"s{i // 10}") for i in range(n)]
    secs = [Section(id=f"s{i}", title=f"Chapter {i}", start_para=i * 10) for i in range(n // 10)]
    chars = [Character(id="a", name="Anna", first_para=0, voice="v", persona="p"),
             Character(id="b", name="Bo", first_para=60, voice="v", persona="p")]
    return Book(id="x", title="X", author="A", kind="novel", license="x", sections=secs, paragraphs=paras, characters=chars,
                summaries=[Summary(upto_para=49, text="early"), Summary(upto_para=79, text="late secret")], series_index=1)


def test_positions_snap_to_chapter_starts():
    b = make_book(120)
    pos = pick_positions(b)
    assert [p["reader_para"] for p in pos] == [50, 90]
    assert pos[0]["chapter"] == "Chapter 5"
    assert all(p["reader_para"] in {s.start_para for s in b.sections} for p in pos)


def test_positions_never_first_chapter_and_distinct():
    pos = pick_positions(make_book(20))
    assert all(p["reader_para"] > 0 for p in pos)
    assert len({p["section_id"] for p in pos}) == len(pos)


def test_cast_unlock():
    b = make_book()
    assert [c.id for c in available_characters(b, 40)] == ["a"]
    assert [c.id for c in available_characters(b, 60)] == ["a", "b"]


def test_prompt_uses_only_summary_at_or_before_position():
    b = make_book()
    u = build_user(b, 40, "Prev: recap", available_characters(b, 40))
    b2 = make_book()
    u2 = build_user(b2, 60, "Prev: recap", available_characters(b2, 60))
    assert "early" in u2 and "late secret" not in u2 and "Prev: recap" in u2
    assert "- b: Bo" in u2 and "- b:" not in u
    assert "Never reveal" in SYSTEM


def _demo(cid):
    return DemoSet.model_validate({"already_read": [], "earlier_book": [{"question": "q", "answer": "a"}],
                                   "future": [], "character": {"character_id": cid, "question": "q"}})


def test_validate_rejects_locked_character_and_drops_earlier_for_book1():
    b = make_book()
    chars = available_characters(b, 40)
    with pytest.raises(ValueError):
        validate(_demo("b"), b, chars)
    assert validate(_demo("a"), b, chars).earlier_book == []


def test_render_has_no_answer_for_future():
    data = [{"title": "X", "positions": [{"chapter": "C", "reader_para": 5, "fraction": 0.4,
             "already_read": [{"question": "q1", "answer": "a1"}], "earlier_book": [],
             "future": [{"question": "fq", "expected": "should decline"}],
             "character": {"id": "a", "name": "Anna", "question": "cq"}}]}]
    md = render_markdown(data)
    assert "fq" in md and "Expected: a1" in md and "Ask Anna: cq" in md
