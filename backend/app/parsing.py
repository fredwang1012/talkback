"""Split raw book text into sections and paragraphs. Shared by preprocess/ingest.py and uploads."""
import re
import unicodedata

from .schema import MathBlock, Paragraph, Section

HEADING_MARK = "\x01"  # prefix pdf_to_text puts on headings found by font size alone
CODE_MARK = "\x02"     # prefix pdf_to_text puts on a program / pseudocode listing (its lines separated by \n)
# prefix pdf_to_text puts on a display equation: EQ_MARK "page x0 y0 x1 y1 garbled" EQ_SEP extracted text.
# \x1c-\x1f count as whitespace for str.split and re's \s, so the separator must be below them.
EQ_MARK = "\x03"
EQ_SEP = "\x04"
_EQ_NUMBER = re.compile(r"\s*\((\d{1,3}(?:\.\d{1,3}){0,2}[a-z]?)\)[.,;]?$")
EQUATION_TEXT = "(equation)"  # the shown text of an equation whose glyphs did not extract at all


def split_equation_number(text: str) -> tuple[str, str | None]:
    """("y = 2x", "3.4") from "y = 2x, (3.4)": the trailing equation number, if any, taken off."""
    m = _EQ_NUMBER.search(text)
    if not m:
        return text, None
    return text[:m.start()].rstrip(" ,;"), m.group(1)


def _equation(block: str, n: int) -> tuple[str, MathBlock] | None:
    """(shown text, MathBlock) from an EQ_MARK block, or None when its header is malformed."""
    head, sep, text = block[1:].partition(EQ_SEP)
    parts = head.split()
    if not sep or len(parts) != 6:
        return None
    try:
        page, bbox, garbled = int(parts[0]), [float(v) for v in parts[1:5]], parts[5] == "1"
    except ValueError:
        return None
    text = normalize(text)
    _, number = split_equation_number(text)
    return text or EQUATION_TEXT, MathBlock(id=f"e{n}", page=page, bbox=bbox, number=number, garbled=garbled)


ROMAN = re.compile(r"^[IVXLC]+\.$")
CURLY = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})
BLANK_LINE = re.compile(r"\n\s*\n")


def normalize(text: str) -> str:
    text = text.translate(CURLY).replace("﻿", "")
    text = re.sub(r"_([^_\n]+)_", r"\1", text)  # Gutenberg italics markers
    return re.sub(r"\s+", " ", text).strip()


_JUNK_CHARS = "•‣◦·∙⁃�"  # bullets and the replacement char
_JUNK_CATEGORIES = {"So", "Sm", "Sk", "Co", "Cf", "Cc", "Cs", "Cn"}  # symbols, private-use, format, control
# a numbered / "Chapter N" title followed by words and then a page number (and maybe a stray "$")
_TRAILING_PAGE = re.compile(r"^((?:(?i:chapter|part|book|section|unit)\s+\w+[:.]?|\d+(?:\.\d+)*\.?)\s+.*?[^\W\d_]\S*)"
                            r"\s+\d{1,4}(?:\s+\W+)?$")


def clean_title(title: str) -> str:
    """A chapter title without stray glyphs (bullets, private-use / math symbols) or a trailing page number.

    "6.4 Variant tf-idf functions 129 $" -> "6.4 Variant tf-idf functions"; "Chapter 12: The Patronus 1989" ->
    "Chapter 12: The Patronus". A page number is only stripped after a numbered / "Chapter N" title with words.
    """
    t = "".join(c for c in title if unicodedata.category(c) not in _JUNK_CATEGORIES and c not in _JUNK_CHARS)
    t = re.sub(r"\s+", " ", t).strip()
    m = _TRAILING_PAGE.match(t)
    return m.group(1) if m else t


def parse(raw: str, start_marker: str | None, end_marker: str | None,
          heading_regex: str | None = None, skip_regex: str | None = None):
    """Split `raw` into sections and paragraphs.

    Gutenberg texts: the body lies between *** START OF / *** END OF and sections are roman-numeral lines.
    Plain texts (textbooks) need no Gutenberg markers.
    """
    raw = raw.replace("\r\n", "\n")
    gutenberg = "*** START OF" in raw
    body_start = raw.index("*** START OF") if gutenberg else 0
    start = body_start
    if start_marker:
        # the TOC lists titles without the exact "I. TITLE" heading twice, so use the LAST occurrence
        occurrences = [m.start() for m in re.finditer(re.escape(start_marker), raw[body_start:])]
        if not occurrences:
            raise ValueError(f"start_marker not found: {start_marker!r}")
        start = body_start + (occurrences[-1] if len(occurrences) > 1 else occurrences[0])
    if end_marker:
        end = raw.index(end_marker, start)
    else:
        end = raw.index("*** END OF", start) if gutenberg else len(raw)
    newline = raw.find("\n", start, end)
    body = raw[(newline if newline >= 0 else start):end] if start_marker else raw[start:end]

    heading = re.compile(heading_regex) if heading_regex else None
    skip = re.compile(skip_regex) if skip_regex else None
    sections: list[Section] = []
    paragraphs: list[Paragraph] = []
    current = None
    equations = 0
    for block in BLANK_LINE.split(body):
        block = block.strip()
        if not block:
            continue
        if block.startswith(EQ_MARK):
            eq = _equation(block, equations + 1)
            if eq is None:
                continue
            equations += 1
            if current is None:
                current = Section(id="s1", title="", start_para=len(paragraphs))
                sections.append(current)
            paragraphs.append(Paragraph(idx=len(paragraphs), section_id=current.id, text=eq[0], kind="math", math=eq[1]))
            continue
        if block.startswith(CODE_MARK):
            code = "\n".join(" ".join(ln.split()) for ln in block[1:].split("\n") if ln.strip())
            if code:
                if current is None:
                    current = Section(id="s1", title="", start_para=len(paragraphs))
                    sections.append(current)
                paragraphs.append(Paragraph(idx=len(paragraphs), section_id=current.id, text=code, kind="code"))
            continue
        if heading and block.count("\n") == 1 and len(block) < 70:
            block = block.replace("\n", " ")  # "CHAPTER 8" on one line, its title on the next
        is_heading = bool(heading.match(block)) if heading else bool(ROMAN.match(block))
        if is_heading:
            title = clean_title(normalize(block.lstrip(HEADING_MARK)))
            current = Section(id=f"s{len(sections) + 1}", title=title, start_para=len(paragraphs))
            sections.append(current)
            continue
        if skip and skip.search(block):
            continue
        if current is None:
            current = Section(id="s1", title="", start_para=len(paragraphs))
            sections.append(current)
        text = normalize(block)
        if text:
            paragraphs.append(Paragraph(idx=len(paragraphs), section_id=current.id, text=text))
    return sections, paragraphs
