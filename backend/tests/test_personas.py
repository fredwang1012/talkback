import pytest

from app.personas import MAX_INSTRUCTIONS_CHARS, build_instructions
from app.schema import Book, Character, Paragraph, Section


def make_book(kind="novel"):
    return Book(
        id="b", title="A Scandal in Bohemia", author="ACD", kind=kind, license="PD",
        sections=[Section(id="s1", title="I.", start_para=0)],
        paragraphs=[Paragraph(idx=0, section_id="s1", text="x")],
        characters=[Character(id="holmes", name="Sherlock Holmes", first_para=0, voice="oliver",
                              persona="Cool, precise, fond of deduction.")],
    )


PASSAGES = [{"para_start": 2, "para_end": 4, "text": "He stood before the fire."}]


def test_character_prompt_has_persona_story_and_passages():
    out = build_instructions(make_book(), "holmes", "Watson visits Holmes.", PASSAGES)
    assert out.startswith('You are Sherlock Holmes from "A Scandal in Bohemia"')
    assert "Cool, precise" in out and "Watson visits Holmes." in out
    assert "[¶3-5] He stood before the fire." in out
    assert "language the reader used" in out
    assert "cannot know yet" in out  # spoiler behaviour is in the prompt


def test_narrator_option_for_novels_is_spoiler_aware():
    out = build_instructions(make_book(), "narrator", "", PASSAGES)
    assert "narrator" in out and "cannot tell it yet" in out
    assert "the story has only just begun" in out


def test_textbook_always_uses_tutor_template_and_has_no_story_so_far():
    out = build_instructions(make_book("textbook"), "narrator", "ignored", PASSAGES)
    assert "nature documentary" in out and "Story so far" not in out and "ignored" not in out


def test_empty_passages_are_explicit():
    assert "(nothing relevant)" in build_instructions(make_book(), "holmes", "s", [])


def test_unknown_speaker_raises():
    with pytest.raises(KeyError):
        build_instructions(make_book(), "nobody", "s", PASSAGES)


def test_length_is_capped():
    big = [{"para_start": 0, "para_end": 1, "text": "word " * 5000}]
    assert len(build_instructions(make_book(), "holmes", "s", big)) <= MAX_INSTRUCTIONS_CHARS
