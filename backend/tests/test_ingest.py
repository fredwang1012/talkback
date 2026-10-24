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
