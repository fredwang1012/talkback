import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "preprocess"))
from chunk_tts import MAX_CHARS, build_tts_text, chunk_paragraph  # noqa: E402

from app.delivery import apply_styles, detect_style, laughter_at, strip_tag_residue, with_laughter  # noqa: E402
from app.enrich.segment import SegItem, SegResult, validate_segments  # noqa: E402
from app.schema import Book, Character, Narrator, Paragraph, Section, Segment  # noqa: E402
from app.tts_cache import align_timestamps  # noqa: E402


def segs(*parts):
    return [Segment(speaker=s, text=t) for s, t in parts]


def style_of(*parts, i=0):
    return detect_style(segs(*parts), i)


@pytest.mark.parametrize("tag,expected", [
    ("Ron whispered.", "whispering"), ("hissed Hermione.", "whispering"), ("he breathed.", "whispering"),
    ("Harry muttered darkly.", "whispering"), ("said Lupin, murmuring.", "whispering"),
    ("Hagrid bellowed.", "shouting"), ("yelled Uncle Vernon in panic.", "shouting"),
    ("he roared.", "shouting"), ("Fred screamed.", "shouting"), ("Hagrid thundered.", "shouting"),
    ("said Ron.", None), ("Ron replied.", None), ("Ron did not whisper.", None),
    ("the crowd roared with laughter.", None),
])
def test_tag_after_dialogue(tag, expected):
    assert style_of(("ron", '"Fine, then."'), ("narrator", tag)) == expected


def test_tag_before_dialogue_and_interrupted_quote():
    assert style_of(("narrator", "Hermione hissed,"), ("hermione", '"Be quiet."'), i=1) == "whispering"
    assert style_of(("narrator", "Hagrid shouted:"), ("hagrid", '"Run!"'), i=1) == "shouting"
    parts = [("ron", '"I think,"'), ("narrator", "Ron whispered,"), ("ron", '"it is here."')]
    assert [detect_style(segs(*parts), i) for i in range(3)] == ["whispering", None, "whispering"]
    # a tag that ended its sentence belongs only to the quote before it
    parts = [("ron", '"Look."'), ("narrator", "Ron whispered. Then Harry nodded."), ("harry", '"Right."')]
    assert [detect_style(segs(*parts), i) for i in range(3)] == ["whispering", None, None]


def test_narration_and_long_clauses_get_no_style():
    assert style_of(("narrator", "Ron whispered to Harry")) is None
    long_tag = "Hermione said that she had heard Ron whisper something to Neville before the lesson began."
    assert style_of(("ron", '"Fine."'), ("narrator", long_tag)) is None
    assert style_of(("ron", '"Fine."')) is None


def test_uppercase_shouting_and_conflicts():
    assert style_of(("vernon", '"I WANT MY LETTER!"')) == "shouting"
    assert style_of(("vernon", '"NO."')) is None  # no exclamation mark
    assert style_of(("ron", '"OK!"')) is None  # too short to call
    assert style_of(("ron", "\"Harry's here!\"")) is None
    assert style_of(("ron", '"STOP!"'), ("narrator", "he whispered.")) is None  # contradictory
    assert style_of(("ron", '"Both."'), ("narrator", "he whispered and shouted.")) is None


def test_apply_styles_keeps_llm_style_unless_rule_fires():
    ss = [Segment(speaker="ron", text='"Hi."', style="whispering"),
          Segment(speaker="narrator", text="said Ron.", style="shouting")]
    apply_styles(ss)
    assert ss[0].style == "whispering" and ss[1].style is None  # narration never carries a style
    ss = [Segment(speaker="ron", text='"Hi."', style="whispering"), Segment(speaker="narrator", text="Ron yelled.")]
    apply_styles(ss)
    assert ss[0].style == "shouting"  # rule overrides the LLM


@pytest.mark.parametrize("text", ['"Ha!" said Ron', "Ha ha, very funny", "Hahaha!", '"Hee hee."', "Ho ho ho!", "Heh.", "HA!"])
def test_laughter_detected(text):
    assert laughter_at(text) is not None


@pytest.mark.parametrize("text", ["Hagrid arrived", "Have you seen it?", "Ho there!", "He said ha", "Hello, Ron", "Her cat"])
def test_laughter_not_detected(text):
    assert laughter_at(text) is None and with_laughter(text) == text


def test_sfx_inserted_before_onomatopoeia_after_quote_only():
    assert with_laughter('"Ha, ha," said Harry.') == '"<|sfx:laughter|>Ha, ha," said Harry.'
    assert build_tts_text("Ha ha!", "", None, sfx=True) == "<|sfx:laughter|>Ha ha!"
    assert build_tts_text("Ha ha!", "", None) == "Ha ha!"  # narrator text never gets an sfx


def test_tts_text_tag_order():
    t = build_tts_text("Run!", "<|prosody:pitch_low|>", "fear", "shouting", sfx=True)
    assert t == "<|prosody:pitch_low|><|style:shouting|><|emotion:fear|>Run!"
    assert build_tts_text("Hi.", "", None, "whispering") == "<|style:whispering|>Hi."
    assert build_tts_text("Hi.", "", "awe") == "<|emotion:awe|>Hi."  # unchanged without a style


def make_book(text, segments):
    book = Book(id="b", title="B", author="A", license="PD", kind="novel", narrator=Narrator(voice="nv"),
                characters=[Character(id="ron", name="Ron", voice="rv", voice_tags="<|prosody:pitch_high|>",
                                    first_para=0, persona="p")],
                sections=[Section(id="s", title="S", start_para=0)],
                paragraphs=[Paragraph(idx=0, section_id="s", text=text, segments=segments)])
    return book, book.paragraphs[0]


def test_chunking_with_styles_keeps_limits_and_text():
    quote = '"' + " ".join(["Do not wake the sleeping dragon."] * 12) + '"'
    text = quote + " Ron whispered."
    book, p = make_book(text, [Segment(speaker="ron", text=quote), Segment(speaker="narrator", text="Ron whispered.")])
    chunks = chunk_paragraph(p, book)
    ron = [c for c in chunks if c.speaker == "ron"]
    assert len(ron) > 1 and p.segments[0].style == "whispering"
    for c in ron:
        assert c.tts_text.startswith("<|prosody:pitch_high|><|style:whispering|>")
        assert c.char_end - c.char_start <= MAX_CHARS  # tags are not counted against the spoken-text limit
    assert all("style:" not in c.tts_text for c in chunks if c.speaker == "narrator")


def test_chunk_ids_and_offsets_do_not_depend_on_style(monkeypatch):
    text = '"Ha!" said Ron. "Come on," Ron yelled.'
    parts = [("ron", '"Ha!"'), ("narrator", "said Ron."), ("ron", '"Come on,"'), ("narrator", "Ron yelled.")]
    book, p = make_book(text, [Segment(speaker=s, text=t) for s, t in parts])
    styled = chunk_paragraph(p, book)
    assert styled[0].tts_text.endswith('"<|sfx:laughter|>Ha!"') and styled[1].tts_text == "said Ron."
    assert styled[2].tts_text == '<|prosody:pitch_high|><|style:shouting|>"Come on,"'
    import app.ttschunks as tc
    monkeypatch.setattr(tc, "apply_styles", lambda segments: None)
    for seg in p.segments:
        seg.style = None
    plain = chunk_paragraph(p, book)
    assert [(c.id, c.char_start, c.char_end) for c in styled] == [(c.id, c.char_start, c.char_end) for c in plain]
    assert plain[2].tts_text == '<|prosody:pitch_high|>"Come on,"'


def test_align_strips_style_and_sfx_residue():
    assert strip_tag_residue("styleshoutingI") == "I"
    assert strip_tag_residue("sfxlaughterHa") == "Ha"
    assert strip_tag_residue("emotioncontemplationprosodypitchlowTo") == "To"
    assert strip_tag_residue("Shouting") == "Shouting" and strip_tag_residue("style") == "style"

    def words(ws):
        return [{"word": w, "start": i * 0.3, "end": i * 0.3 + 0.25} for i, w in enumerate(ws)]

    plain = '"I WANT MY LETTER!"'
    out = align_timestamps(words(["styleshoutingI", "WANT", "MY", "LETTER"]), plain)
    assert [plain[o["char_start"]:o["char_end"]] for o in out] == ['"I', "WANT", "MY", 'LETTER!"']
    plain = "Ha! Snape's not going to be pleased."
    out = align_timestamps(words(["sfxlaughterHa", "Snape's", "not", "going", "to", "be", "pleased"]), plain)
    assert [plain[o["char_start"]:o["char_end"]] for o in out][:2] == ["Ha!", "Snape's"]
    tagged = [{"word": "<|style:whispering|>", "start": 0, "end": 0},
              {"word": "stylewhispering", "start": 0, "end": 0}] + words(["Hi"])
    assert len(align_timestamps(tagged, "Hi.")) == 1


def test_enrich_segment_schema_accepts_style():
    assert SegItem(speaker="ron", text="x", emotion=None).style is None  # optional
    items = [SegItem(speaker="ron", text='"Hush."', emotion=None, style="whispering"),
             SegItem(speaker="narrator", text="said Ron.", emotion=None, style="shouting")]
    out = validate_segments('"Hush." said Ron.', items, {"ron", "narrator"})
    assert [s.style for s in out] == ["whispering", None]  # narration style dropped
    with pytest.raises(Exception):
        SegItem(speaker="ron", text="x", emotion=None, style="singing")
    assert "style" in SegResult.model_json_schema()["$defs"]["SegItem"]["properties"]


# ---------- natural reading speed (#78) ----------

def test_apply_speed_puts_the_tag_first_and_keeps_the_rest():
    from app.delivery import apply_speed
    text = "<|prosody:pitch_low|><|style:whispering|><|prosody:pause|>Hello there."
    out = apply_speed(text, "slow")
    assert out == "<|prosody:speed_slow|>" + text
    assert out.endswith("Hello there.")


def test_apply_speed_none_is_identity_and_replaces_a_voice_speed_tag():
    from app.delivery import apply_speed
    assert apply_speed("Hi.", None) == "Hi."
    assert apply_speed("<|prosody:speed_fast|>Hi.", "slow") == "<|prosody:speed_slow|>Hi."


def test_apply_speed_rejects_unknown_values():
    import pytest
    from app.delivery import apply_speed
    with pytest.raises(ValueError):
        apply_speed("Hi.", "warp")
