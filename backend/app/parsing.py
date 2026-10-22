"""Split raw book text into sections and paragraphs. Shared by preprocess/ingest.py and uploads."""
import re

from .schema import Paragraph, Section

HEADING_MARK = "\x01"  # prefix pdf_to_text puts on headings found by font size alone
CODE_MARK = "\x02"     # prefix pdf_to_text puts on a program / pseudocode listing (its lines separated by \n)
ROMAN = re.compile(r"^[IVXLC]+\.$")
CURLY = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"'})
BLANK_LINE = re.compile(r"\n\s*\n")


def normalize(text: str) -> str:
    text = text.translate(CURLY).replace("﻿", "")
    text = re.sub(r"_([^_\n]+)_", r"\1", text)  # Gutenberg italics markers
    return re.sub(r"\s+", " ", text).strip()


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
    for block in BLANK_LINE.split(body):
        block = block.strip()
        if not block:
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
            title = normalize(block.lstrip(HEADING_MARK))
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
