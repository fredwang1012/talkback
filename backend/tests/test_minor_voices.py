"""Uncast speakers get one of two generic voices by inferred gender (#70)."""
from app.enrich import minor
from app.enrich.minor import MINOR_VOICES, assign_minor_voices, infer_gender
from app.schema import Book, Character, Narrator, Paragraph, Section, Segment
from app.ttschunks import chunk_paragraph


def make_book(paras: list[str]) -> Book:
    ps = [Paragraph(idx=i, section_id="s1", text=t, segments=[Segment(speaker="narrator", text=t)]) for i, t in enumerate(paras)]
    b = Book(id="b", title="T", author="A", kind="novel", license="x", sections=[Section(id="s1", title="1", start_para=0)],
             paragraphs=ps, narrator=Narrator(voice="marcus"),
             characters=[Character(id="harry", name="Harry", first_para=0, voice="jake", persona="p")])
    for p in ps:
        p.tts_chunks = chunk_paragraph(p, b)
    return b


def test_gender_from_pronoun_title_noun_and_default():
    assert infer_gender("", ', she said quietly.') == "female"
    assert infer_gender("", ", he said.") == "male"
    assert infer_gender("", ", said Mrs. Dursley stiffly.") == "female"
    assert infer_gender("The old woman said,", "") == "female"
    assert infer_gender("", ", said Zorp.") == minor.UNKNOWN_GENDER


def test_two_generic_voices_are_lower_and_higher_presets():
    from app import voices
    m, f = MINOR_VOICES["male"], MINOR_VOICES["female"]
    assert m.voice in voices.PRESET_VOICES and f.voice in voices.PRESET_VOICES
    assert "pitch_low" in m.tags and "pitch_high" in f.tags


def test_uncast_lines_get_generic_voices_and_cast_lines_are_untouched():
    b = make_book(['"So?" snapped Mrs. Dursley.', '"Shoo!" said Mr. Dursley loudly.', '"Hi," said Harry.'])
    lines = assign_minor_voices(b)
    assert len(lines) == 2
    assert [s.speaker for s in b.paragraphs[0].segments][0] == "minor_f"
    assert [s.speaker for s in b.paragraphs[1].segments][0] == "minor_m"
    assert b.paragraphs[2].segments[0].speaker == "harry"  # cast lines go to the cast, not the stand-ins
    by = {c.id: c for c in b.characters}
    assert by["minor_f"].main is False and by["minor_f"].voice == MINOR_VOICES["female"].voice
    c0 = b.paragraphs[0].tts_chunks[0]
    assert c0.speaker == "minor_f" and c0.voice == MINOR_VOICES["female"].voice
    assert c0.tts_text.startswith(MINOR_VOICES["female"].tags)
    text = "".join(s.text for s in b.paragraphs[0].segments)
    assert text.replace(" ", "") == b.paragraphs[0].text.replace(" ", "")  # segments still tile the paragraph


def test_idempotent_and_dry_run_changes_nothing():
    b = make_book(['"So?" snapped Mrs. Dursley.'])
    assert len(assign_minor_voices(b, apply=False)) == 1
    assert [c.id for c in b.characters] == ["harry"] and b.paragraphs[0].segments[0].speaker == "narrator"
    assign_minor_voices(b)
    assert assign_minor_voices(b) == [] and [c.id for c in b.characters].count("minor_f") == 1


def test_book_wide_evidence_resolves_an_unknown_name():
    b = make_book(['"One," said Zorp.', '"Two," she said to Zorp.', '"Two," said Zorp, she said.',
                   '"Three," said Zorp.'])
    # no pronoun at the last line, but earlier lines tied "Zorp" to she
    assign_minor_voices(b)
    assert b.paragraphs[3].segments[0].speaker in ("minor_m", "minor_f")

