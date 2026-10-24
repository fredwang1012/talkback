"""Spoken math for TTS: converted in textnorm only, so the displayed paragraph text is untouched."""
import time

from app.schema import Book, Narrator, Paragraph, Section
from app.textnorm import normalize_for_tts, speak_math
from app.ttschunks import MAX_CHARS, chunk_paragraph


def test_simple_conversions():
    assert speak_math("the area is x² here") == "the area is x squared here"
    assert speak_math("then y³ grows") == "then y cubed grows"
    assert speak_math("take √x now") == "take square root of x now"
    assert speak_math("so a/b is small") == "so a over b is small"
    assert speak_math("if x ≤ 5 holds") == "if x less than or equal to 5 holds"
    assert speak_math("and y ≥ 2 too") == "and y greater than or equal to 2 too"
    assert speak_math("Energy E = mc² is famous.") == "Energy E equals mc squared is famous."
    assert speak_math("the angle θ is 30°") == "the angle theta is 30 degrees"
    assert speak_math("a + b × c") == "a plus b times c"
    assert speak_math("use x^2 and x^n") == "use x squared and x to the n"
    assert speak_math("a rate of Δx / Δt") == "a rate of delta x over delta t"


def test_greek_names():
    assert speak_math("the constant π and λ") == "the constant pi and lambda"


def test_complex_equations_become_a_marker():
    assert speak_math("We get ∑ x_i² = ∫ f(x) dx = 1 here.") == "We get (equation) here."
    assert speak_math("see { a = 1, b = 2 } ok") == "see (equation) ok"


def test_prose_is_left_alone():
    for s in ("Use and/or as needed, w/o fuss.", "A man, a plan, a canal.", "It cost C++ users 3 days.",
              "Pages 3 - 4 cover it, e.g. the 50% case.", "He said that is fine."):
        assert speak_math(s) == s


def test_footnote_style_superscript_is_not_math():
    s = "The membrane¹ is thin."
    assert speak_math(s) == s


def test_normalize_chains_math_then_numbers():
    assert normalize_for_tts("so x² = 4 in 1999") == "so x squared equals four in nineteen ninety-nine"


def test_hostile_input_is_fast():
    t = time.perf_counter()
    speak_math("= " * 100_000)
    speak_math("x² " * 100_000)
    speak_math("a" * 300_000)
    assert time.perf_counter() - t < 3


def test_display_text_and_chunk_invariants_unchanged():
    text = ("Recall that x² + y² = r² for a circle. " * 8) + "The slope is a/b and √x grows. " * 6
    p = Paragraph(idx=0, section_id="s1", text=text)
    book = Book(id="b", title="t", author="a", kind="textbook", license="x",
                sections=[Section(id="s1", title="", start_para=0)], paragraphs=[p],
                narrator=Narrator(voice="marcus"))
    chunks = chunk_paragraph(p, book)
    assert p.text == text and "²" in p.text
    assert all(len(p.text[c.char_start:c.char_end]) <= MAX_CHARS for c in chunks)
    assert "squared" in chunks[0].tts_text and "²" not in chunks[0].tts_text


def test_combining_not_equal():
    assert speak_math("p1\u0338 = NIL") == "p1 not equal to NIL"
    assert speak_math("p1 =\u0338 NIL") == "p1 not equal to NIL"
    assert speak_math("p1\u0338= NIL") == "p1 not equal to NIL"


def test_assignment_arrow():
    assert speak_math("x ← 5") == "x gets 5"


def test_dotted_section_numbers():
    assert normalize_for_tts("See section 5.3.1 for details.") == "See section five point three point one for details."
    assert normalize_for_tts("Section 2.1.1.4 here") == "Section two point one point one point four here"
    assert normalize_for_tts("pi is 3.14 roughly") == "pi is three.fourteen roughly"  # ordinary decimals untouched


def test_may_change_is_a_safe_precheck_for_normalization():
    """Whenever may_change says no, normalize_for_tts leaves the text exactly as it is (checked on every committed
    book paragraph and some edge cases)."""
    import json
    from pathlib import Path
    from app.textnorm import may_change, normalize_for_tts
    texts = ["a/b and w/o", "x² + y", "α beta", "Mr. Smith-Jones (aged) — fine; ok!", "{a}", "p\u0338=q", "x^2", "100"]
    for f in sorted((Path(__file__).resolve().parents[2] / "data" / "books").glob("*/book.json")):
        texts += [p["text"] for p in json.loads(f.read_text(encoding="utf-8"))["paragraphs"]]
    for t in texts:
        if not may_change(t):
            assert normalize_for_tts(t) == t, t[:80]
    assert may_change("a/b") and may_change("x²") and may_change("100") and not may_change("Plain words, only.")
