"""#73: more sound effects, pauses and expressiveness, all rule-based (no LLM)."""
import pytest

from app.delivery import detect_expressive, is_scene_break, sfx_at, speech_tag, strip_tag_residue, with_sfx
from app.schema import Book, Character, Narrator, Paragraph, Section, Segment
from app.tts_cache import align_timestamps
from app.ttschunks import build_tts_text, chunk_paragraph


def segs(*parts):
    return [Segment(speaker=s, text=t) for s, t in parts]


def sfx_of(line, tag=""):
    found = sfx_at(line, tag)
    return found[1] if found else None


# ---------- sound effects ----------

@pytest.mark.parametrize("line,tag,expected", [
    ('"Sigh."', "she sighed.", "sigh"),
    ('"Ahh-h, well,"', "Alice sighed,", "sigh"),
    ('"Haah. Fine."', "sighed the Rat.", "sigh"),
    ('"Ahh, so that is it."', "said the Mole.", None),  # no sighed: no sound
    ('"Boo-hoo!"', "Alice sobbed.", "crying"),
    ('"Boo hoo!"', "she wept.", "crying"),
    ('"Sob! I lost it."', "she cried bitterly.", "crying"),
    ('"Boo-hoo!"', "cried Alice.", None),  # 'cried' meaning said
    ('"Boo-hoo!"', "", None),
    ('"Sob!"', "she was not sobbing.", None),
    ('"Sniff!"', "", "sniff"), ('"Sniff, sniff."', "", "sniff"),
    ('"Sniff this rose."', "", None),  # a verb, not a sound
    ('"Ahem! Attention, please."', "", "cough"), ('"Cough, cough."', "", "cough"),
    ('"Cough it up."', "", None),
    ('"AAAAH!"', "", "screaming"), ('"AAAARGH!"', "", "screaming"), ('"Aah!"', "Wendy screamed.", "screaming"),
    ('"Aaah!"', "", None), ('"Aaargh!"', "said Ron.", None),  # lowercase, no scream in the tag: delight? pain?
    ('"Aaah."', "", None),  # no exclamation
    ('"Ha ha!"', "", "laughter"), ('"Ha-ha!"', "", "laughter"),
    ('"I sighed all day."', "she sighed.", None), ('"Where is it?"', "she sobbed.", None),  # no onomatopoeia
])
def test_sfx_rules(line, tag, expected):
    assert sfx_of(line, tag) == expected


def test_sfx_goes_right_before_the_sound_and_text_is_kept():
    assert with_sfx('"Boo-hoo!" she sobbed.', "she sobbed.") == '"<|sfx:crying|>Boo-hoo!" she sobbed.'
    assert with_sfx('"Ahem!"') == '"<|sfx:cough|>Ahem!"'
    assert with_sfx('"Hello."', "she sighed.") == '"Hello."'


def test_speech_tag_takes_short_adjacent_tags_only():
    ss = segs(("narrator", "Alice sighed,"), ("alice", '"Ahh-h."'), ("narrator", "she said. Then she left the room."))
    assert speech_tag(ss, 1) == "she said | Alice sighed,"
    long_narr = "The Rat, who had been rowing all afternoon with the Mole beside him, sighed deeply,"
    assert speech_tag(segs(("narrator", long_narr), ("rat", '"Ahh-h."')), 1) == ""


def test_sfx_is_character_speech_only_in_chunks():
    book, p = make_book('"Sniff!" The dog went sniff.', [Segment(speaker="ron", text='"Sniff!"'),
                                                         Segment(speaker="narrator", text="The dog went sniff.")])
    p.idx = 1  # not a section start
    book.paragraphs.insert(0, Paragraph(idx=0, section_id="s", text="Ron came in", segments=[]))
    chunks = chunk_paragraph(p, book)
    assert chunks[0].tts_text.endswith('"<|sfx:sniff|>Sniff!"') and "sfx" not in chunks[1].tts_text
    assert build_tts_text("Sniff!", "", None) == "Sniff!"  # narrator (sfx=False) never gets one


def test_chunk_uses_narration_tag_for_sfx():
    text = '"Boo-hoo!" Alice sobbed.'
    book, p = make_book(text, segs(("ron", '"Boo-hoo!"'), ("narrator", "Alice sobbed.")))
    assert "<|sfx:crying|>Boo-hoo!" in chunk_paragraph(p, book)[0].tts_text
    book, p = make_book('"Boo-hoo!" cried Ron.', segs(("ron", '"Boo-hoo!"'), ("narrator", "cried Ron.")))
    assert "sfx" not in chunk_paragraph(p, book)[0].tts_text


# ---------- expressiveness ----------

def test_expressive_high_for_climactic_lines():
    assert detect_expressive(segs(("ron", '"I WANT MY LETTER!"')), 0) == "expressive_high"
    assert detect_expressive(segs(("ron", '"Get out!"'), ("narrator", "Ron shouted.")), 0) == "expressive_high"
    assert detect_expressive(segs(("narrator", "Wendy screamed:"), ("ron", '"Look out!"')), 1) == "expressive_high"


def test_expressive_negatives():
    assert detect_expressive(segs(("ron", '"Get out."'), ("narrator", "Ron shouted.")), 0) is None  # no "!"
    assert detect_expressive(segs(("ron", '"Get out!"'), ("narrator", "Ron said.")), 0) is None
    assert detect_expressive(segs(("ron", '"Get out!"'), ("narrator", "Ron roared.")), 0) is None  # only shouted/screamed
    assert detect_expressive(segs(("narrator", "THE END!")), 0) is None  # narration


def test_expressive_low_for_whispered_lines_with_style():
    book, p = make_book('"Quiet now," Ron whispered.', segs(("ron", '"Quiet now,"'), ("narrator", "Ron whispered.")))
    t = chunk_paragraph(p, book)[0].tts_text
    assert t.startswith("<|prosody:pitch_high|><|style:whispering|><|prosody:expressive_low|>")
    assert t.count("expressive") == 1


def test_no_double_expressive_tag():
    assert build_tts_text("Hi!", "<|prosody:expressive_high|>", None, expressive="expressive_high") \
        == "<|prosody:expressive_high|>Hi!"
    assert build_tts_text("Hi!", "<|prosody:expressive_low|>", None, expressive="expressive_high") \
        == "<|prosody:expressive_low|>Hi!"


def test_tag_order():
    t = build_tts_text('"Ha ha!"', "<|prosody:pitch_low|>", "anger", "shouting", sfx=True,
                       expressive="expressive_high", pause="pause")
    assert t == ('<|prosody:pitch_low|><|style:shouting|><|emotion:anger|><|prosody:expressive_high|>'
                 '<|prosody:pause|>"<|sfx:laughter|>Ha ha!"')


# ---------- pauses ----------

@pytest.mark.parametrize("text,expected", [
    ("* * *", True), ("***", True), ("#", True), ("* * * * * * *", True), ("~ ~ ~", True),
    ("I. Chapter", False), ("1", False), ("-", False), ("", False), ("Ha * ha", False),
])
def test_scene_break(text, expected):
    assert is_scene_break(text) == expected


def make_book(text, segments, more=()):
    book = Book(id="b", title="B", author="A", license="PD", kind="novel", narrator=Narrator(voice="nv"),
                characters=[Character(id="ron", name="Ron", voice="rv", voice_tags="<|prosody:pitch_high|>",
                                      first_para=0, persona="p")],
                sections=[Section(id="s", title="S", start_para=0)],
                paragraphs=[Paragraph(idx=0, section_id="s", text=text, segments=segments)])
    for i, (t, ss) in enumerate(more, start=1):
        book.paragraphs.append(Paragraph(idx=i, section_id="s", text=t, segments=ss))
    return book, book.paragraphs[0]


def first_tts(book, i):
    return chunk_paragraph(book.paragraphs[i], book)[0].tts_text


def test_long_pause_after_chapter_heading_and_scene_break():
    book, _ = make_book("It was night.", [], more=[
        ("* * *", []), ("Morning came.", []), ("Then noon.", [])])
    book.sections.append(Section(id="s2", title="II", start_para=3))
    assert first_tts(book, 0) == "<|prosody:long_pause|>It was night."  # first paragraph after its heading
    assert first_tts(book, 1) == "* * *"  # the separator itself is left alone
    assert first_tts(book, 2) == "<|prosody:long_pause|>Morning came."
    assert first_tts(book, 3) == "<|prosody:long_pause|>Then noon."


def test_long_pause_only_on_first_chunk():
    long = " ".join(["The river ran on and on past the willows."] * 10)
    book, p = make_book(long, [])
    chunks = chunk_paragraph(p, book)
    assert len(chunks) > 1 and chunks[0].tts_text.startswith("<|prosody:long_pause|>")
    assert all("pause" not in c.tts_text for c in chunks[1:])


def test_short_pause_between_narration_and_dialogue():
    book, _ = make_book("Intro.", [], more=[
        ("The Rat sat down.", segs(("narrator", "The Rat sat down."))),
        ('"Well?"', segs(("ron", '"Well?"'))),
        ('"And then?"', segs(("ron", '"And then?"'))),  # dialogue after dialogue: no pause
        ("He turned and said:", segs(("narrator", "He turned and said:"))),
        ('"Go."', segs(("ron", '"Go."'))),  # narration introduces the quote: no pause
        ("The Mole nodded.", segs(("narrator", "The Mole nodded."))),
        ("Nothing more was said.", segs(("narrator", "Nothing more was said."))),  # narration after narration
    ])
    assert first_tts(book, 2) == '<|prosody:pitch_high|><|prosody:pause|>"Well?"'
    assert "pause" not in first_tts(book, 3)
    assert "pause" not in first_tts(book, 5)
    assert "pause" not in first_tts(book, 7)


def test_expressive_false_reproduces_old_tts_text():
    book, p = make_book('"Boo-hoo!" Ron sobbed.', segs(("ron", '"Boo-hoo!"'), ("narrator", "Ron sobbed.")))
    old = chunk_paragraph(p, book, expressive=False)
    assert old[0].tts_text == '<|prosody:pitch_high|>"Boo-hoo!"'
    new = chunk_paragraph(p, book)
    assert [(c.id, c.char_start, c.char_end) for c in old] == [(c.id, c.char_start, c.char_end) for c in new]


# ---------- timestamp residue ----------

@pytest.mark.parametrize("word,clean", [
    ("prosodylongpauseMorning", "Morning"), ("prosodypauseWell", "Well"), ("prosodyexpressivehighGet", "Get"),
    ("prosodyexpressivelowQuiet", "Quiet"), ("sfxsighAhh-h", "Ahh-h"), ("sfxcryingBoo-hoo", "Boo-hoo"),
    ("sfxsniffSniff", "Sniff"), ("sfxcoughAhem", "Ahem"), ("sfxscreamingAaah", "Aaah"), ("sfxcoughcough", "cough"),
    ("stylewhisperingprosodyexpressivelowprosodypauseHush", "Hush"),
])
def test_residue_of_new_tags_is_stripped(word, clean):
    assert strip_tag_residue(word) == clean


def test_alignment_with_new_tag_residue():
    def words(ws):
        return [{"word": w, "start": i * 0.3, "end": i * 0.3 + 0.25} for i, w in enumerate(ws)]

    plain = '"Boo-hoo! I lost it."'
    out = align_timestamps(words(["prosodypausesfxcryingBoo-hoo", "I", "lost", "it"]), plain)
    assert [plain[o["char_start"]:o["char_end"]] for o in out] == ['"Boo-hoo!', "I", "lost", 'it."']
    plain = "Morning came."
    tagged = [{"word": "<|prosody:long_pause|>", "start": 0, "end": 0}] + words(["prosodylongpauseMorning", "came"])
    out = align_timestamps(tagged, plain)
    assert [plain[o["char_start"]:o["char_end"]] for o in out] == ["Morning", "came."]


def test_scene_break_check_is_linear_and_length_capped():
    import time
    from app.delivery import is_scene_break
    t = time.perf_counter()
    assert not is_scene_break("* " * 50000 + "x")
    assert time.perf_counter() - t < 0.5
    assert not is_scene_break("*" * 41)


# ---------- sneeze and humming (#135) ----------

@pytest.mark.parametrize("line,tag,expected", [
    ('"Achoo!"', "", "sneeze"), ('"Ah-choo! Bless me."', "", "sneeze"), ('"A-tchoo!"', "he sneezed.", "sneeze"),
    ('"Atishoo, atishoo."', "", "sneeze"),
    ('"Achoo" is a sound.', "", "sneeze"),  # starts with the sound: the same rule as every other sfx
    ('"Choose one."', "", None), ('"Ah, choo-choo train!"', "", None),
    ('"Hmm-hmm-hmm."', "she hummed.", "humming"), ('"Mmm, mmm."', "Pooh hummed to himself.", "humming"),
    ('"Hmmm, hm hm."', "humming a tune,", "humming"),
    ('"Hmm."', "", None), ('"Hmm, I wonder."', "said Ron.", None),  # thinking, not humming
    ('"Hmm."', "she hummed.", "humming"),
    ('"Hmm."', "she did not hum.", None),
    ('"Mmmm! Delicious."', "", None),
])
def test_sneeze_and_humming_rules(line, tag, expected):
    assert sfx_of(line, tag) == expected


def test_only_documented_sfx_names_are_emitted():
    from app.delivery import TAG_VALUES
    for line, tag in [('"Achoo!"', ""), ('"Hmm."', "she hummed."), ('"Boo-hoo!"', "she wept."), ('"Ahem!"', ""),
                      ('"Sniff!"', ""), ('"AAAAH!"', ""), ('"Ha ha!"', ""), ('"Sigh."', "she sighed.")]:
        assert sfx_of(line, tag) in TAG_VALUES["sfx"]


def test_one_sfx_per_chunk_and_none_in_songs():
    out = with_sfx('"Achoo! Sniff! Ahem!"')
    assert out.count("<|sfx:") == 1
    book, p = make_book('"Achoo!" said Ron.', segs(("ron", '"Achoo!"'), ("narrator", "said Ron.")))
    assert "<|sfx:sneeze|>" in chunk_paragraph(p, book)[0].tts_text
    song = '"Achoo! Beautiful Soup, so rich and green, Waiting in a hot tureen!"'
    sung = [Paragraph(idx=0, section_id="s", text="CHORUS.", segments=segs(("narrator", "CHORUS."))),
            Paragraph(idx=1, section_id="s", text=song, segments=segs(("ron", song)))]
    book, _ = make_book("CHORUS.", sung[0].segments, more=[(song, sung[1].segments)])
    tts = chunk_paragraph(book.paragraphs[1], book)[0].tts_text
    assert "<|style:singing|>" in tts and "<|sfx:" not in tts


def test_chunk_rules_version_covers_new_sfx():
    from app.ttschunks import CHUNK_RULES_VERSION
    assert CHUNK_RULES_VERSION >= 2
