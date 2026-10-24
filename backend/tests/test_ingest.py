import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "preprocess"))
from ingest import normalize, parse  # noqa: E402

RAW = """header
*** START OF THE PROJECT GUTENBERG EBOOK X ***

CONTENTS
I. TITLE ONE
II. TITLE TWO

I. TITLE ONE


I.

To Holmes she is always _the_ woman. I have
seldom heard him.

“Yes,” said he. ‘Quite so.’

II.

Second section text.

II. TITLE TWO

Other story.
*** END OF THE PROJECT GUTENBERG EBOOK X ***
"""


def test_parse_sections_and_paragraphs():
    sections, paras = parse(RAW, "I. TITLE ONE", "II. TITLE TWO")
    assert [s.title for s in sections] == ["I.", "II."]
    assert [s.start_para for s in sections] == [0, 2]
    assert [p.text for p in paras] == [
        "To Holmes she is always the woman. I have seldom heard him.",
        "\"Yes,\" said he. 'Quite so.'",
        "Second section text.",
    ]
    assert [p.idx for p in paras] == [0, 1, 2]
    assert paras[2].section_id == "s2"


def test_normalize_whitespace_and_quotes():
    assert normalize("  a\n  b\t“c” ") == 'a b "c"'


TEXTBOOK = """Chapter 8 Photosynthesis

8.1 Overview of Photosynthesis

Plants make food from light. This is called photosynthesis.

Figure 8.2 A leaf. Credit: someone.

Chloroplasts hold the pigments.

8.2 The Light-Dependent Reactions

Light is absorbed by chlorophyll.
"""


def test_textbook_without_gutenberg_markers_uses_headings_and_skips_credits():
    sections, paras = parse(TEXTBOOK, None, None, r"^8\.\d+\s+\S", r"^Figure \d")
    titles = [s.title for s in sections]
    assert "8.1 Overview of Photosynthesis" in titles and "8.2 The Light-Dependent Reactions" in titles
    texts = [p.text for p in paras]
    assert "Plants make food from light. This is called photosynthesis." in texts
    assert not any(t.startswith("Figure") for t in texts)
    by_section = {s.id: [p.text for p in paras if p.section_id == s.id] for s in sections}
    second = next(s for s in sections if s.title.startswith("8.2"))
    assert by_section[second.id] == ["Light is absorbed by chlorophyll."]
