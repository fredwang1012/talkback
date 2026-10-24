"""Recap scopes (#89): the last chapter, this book so far, the series so far. Built only from stored summaries.

Spoiler rule: every scope uses only summaries with `upto_para <= reader_para` (earlier books of a series are fully
read, so their closing summaries are fine). No model call: the stored summaries are "story so far" notes, so the last
chapter's part is cut out of them:
  - uploaded books and most built-in ones mark it ("... The current chapter so far: <note>"): the part after the mark;
  - rolling notes without the mark (offline preprocess): the sentences of the chapter's last note that the note before
    the chapter did not already say (word overlap), else the whole note;
  - the first chapter: its note is all about that chapter.

Textbooks (notes from enrich/textbook.py, each tagged with its `section_id`) have no spoilers: "chapter" is the note
of the chapter the reader is in, or of any chapter / subsection asked for (`section`), wherever the reader is; "book"
lists the chapters up to the reader's. A textbook without such notes, and every novel, keeps the rule above, and a
novel's `section` recap is still built only from notes with `upto_para <= reader_para`.
"""
import re
from typing import Literal

from . import books
from .schema import Book, Section, Summary

Scope = Literal["chapter", "book", "series"]
SCOPES: tuple[str, ...] = ("chapter", "book", "series")

CHAPTER_MARK = "The current chapter so far:"
BOOK_WORDS = 300    # textbook "book" scope: the chapters' opening sentences, most recent chapters kept first
BOOK_SENTENCES = 2
OLD_OVERLAP = 0.6   # a sentence whose content words were this much in the earlier note is not new
MIN_SENTENCE_WORDS = 4

_SENTENCE = re.compile(r"(?<!\bMr\.)(?<!\bMrs\.)(?<!\bMs\.)(?<!\bDr\.)(?<!\bSt\.)(?<=[.!?])\s+")


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z']+", text.lower()) if len(w) > 3}


def latest_summary(book: Book, upto: int, after: int = -1) -> Summary | None:
    """The latest summary with after < upto_para <= upto."""
    ok = [s for s in book.summaries if after < s.upto_para <= upto]
    return max(ok, key=lambda s: s.upto_para) if ok else None


def new_sentences(text: str, before: str) -> str:
    """Sentences of `text` the earlier note `before` did not already say."""
    seen = _words(before)
    out = []
    for s in (x.strip() for x in _SENTENCE.split(text.strip())):
        w = _words(s)
        if len(s.split()) >= MIN_SENTENCE_WORDS and w and len(w & seen) / len(w) < OLD_OVERLAP:
            out.append(s)
    return " ".join(out)


def chapter_note(book: Book, summary: Summary, chapter_start: int) -> str:
    """The part of `summary` (a note inside the chapter that starts at `chapter_start`) about that chapter only."""
    text = summary.text.strip()
    if CHAPTER_MARK in text:
        return text.split(CHAPTER_MARK, 1)[1].strip()
    prev = latest_summary(book, chapter_start - 1)
    if prev is None:
        return text
    return new_sentences(text, prev.text) or text


def _sections(book: Book, para: int) -> tuple[list[Section], int]:
    secs = sorted(book.sections, key=lambda s: s.start_para)
    cur = max((i for i, s in enumerate(secs) if s.start_para <= para), default=-1)
    return secs, cur


def chapter_recap(book: Book, para: int) -> dict:
    """The last chapter the reader finished; in the first chapter (or when that one has no note), this one so far."""
    secs, cur = _sections(book, para)
    if cur < 0:
        return {"upto_para": None, "chapter": "", "text": ""}
    for i in (cur - 1, cur):
        if i < 0:
            continue
        end = secs[i + 1].start_para - 1 if i + 1 < len(secs) else para
        s = latest_summary(book, min(end, para), secs[i].start_para - 1)
        if s is not None:
            return {"upto_para": s.upto_para, "chapter": secs[i].title, "text": chapter_note(book, s, secs[i].start_para)}
    return {"upto_para": None, "chapter": secs[cur].title, "text": ""}


def section_recap(book: Book, para: int, section_id: str) -> dict:
    """Novel: the note of chapter `section_id` as far as the reader has read it (never a note beyond `para`)."""
    secs = sorted(book.sections, key=lambda s: s.start_para)
    i = next((k for k, s in enumerate(secs) if s.id == section_id), None)
    if i is None:
        return {"upto_para": None, "chapter": "", "text": ""}
    end = secs[i + 1].start_para - 1 if i + 1 < len(secs) else len(book.paragraphs) - 1
    s = latest_summary(book, min(end, para), secs[i].start_para - 1)
    if s is None:
        return {"upto_para": None, "chapter": secs[i].title, "text": ""}
    return {"upto_para": s.upto_para, "chapter": secs[i].title, "text": chapter_note(book, s, secs[i].start_para)}


# ---------- textbooks ----------

def textbook_notes(book: Book) -> dict[str, Summary]:
    """{section_id: note} of a textbook's study notes; empty for novels and for textbooks without them."""
    if book.kind != "textbook":
        return {}
    return {s.section_id: s for s in book.summaries if s.section_id}


def _note_of(book: Book, notes: dict[str, Summary], section_id: str) -> tuple[Section, Summary] | None:
    """The note of a section, or of the subsection / chapter it belongs to when it has none (e.g. "4.2.1")."""
    secs = sorted(book.sections, key=lambda s: s.start_para)
    i = next((k for k, s in enumerate(secs) if s.id == section_id), None)
    if i is None:
        return None
    level = secs[i].level
    for j in range(i, -1, -1):   # this section, then each enclosing one
        if j != i and secs[j].level >= level:
            continue
        if secs[j].id in notes:
            return secs[j], notes[secs[j].id]
        level = min(level, secs[j].level)
        if level <= 1:
            break
    return None


def _chapter_at(book: Book, para: int) -> Section | None:
    tops = [s for s in book.sections if s.level <= 1 and s.start_para <= para]
    return max(tops, key=lambda s: s.start_para) if tops else None


def _textbook_scoped(book: Book, para: int, scope: Scope, notes: dict[str, Summary], section: str | None) -> dict:
    if scope == "chapter":
        if section:
            found = _note_of(book, notes, section)
        else:
            chap = _chapter_at(book, para)
            found = _note_of(book, notes, chap.id) if chap else None
            if found is None:   # the chapter has no note: the reader's subsection's
                secs, cur = _sections(book, para)
                found = _note_of(book, notes, secs[cur].id) if cur >= 0 else None
        if found is None:
            return {"scope": scope, "upto_para": None, "chapter": "", "text": ""}
        sec, note = found
        return {"scope": scope, "upto_para": note.upto_para, "chapter": sec.title, "text": note.text.strip()}
    chap = _chapter_at(book, para)
    tops = sorted((s for s in book.sections if s.level <= 1 and s.start_para <= para and s.id in notes),
                  key=lambda s: s.start_para)
    lines, budget = [], BOOK_WORDS
    for s in reversed(tops):   # the most recent chapters first, while the word budget lasts
        first = " ".join(x.strip() for x in _SENTENCE.split(notes[s.id].text.strip())[:BOOK_SENTENCES])
        line = f"{s.title}: {first}"
        if lines and len(line.split()) > budget:
            break
        lines.append(line)
        budget -= len(line.split())
    lines.reverse()
    upto = max((notes[s.id].upto_para for s in tops), default=None)
    out = {"scope": scope, "upto_para": upto, "chapter": chap.title if chap else "", "text": "\n".join(lines)}
    if scope == "series":
        earlier = books.series_recap(book)
        mine = f"This book so far: {out['text']}" if out["text"] else ""
        out["text"] = "\n".join(t for t in (earlier, mine) if t)
    return out


def scoped(book: Book, para: int, scope: Scope, section: str | None = None) -> dict:
    """{scope, upto_para, chapter, text} for a clamped reader `para`. `upto_para` is the summary the text came from.
    `section` (chapter scope): that chapter instead of the last one (novels: only as far as the reader has read)."""
    notes = textbook_notes(book)
    if notes:
        return _textbook_scoped(book, para, scope, notes, section)
    if scope == "chapter":
        if section:
            return {"scope": scope, **section_recap(book, para, section)}
        return {"scope": scope, **chapter_recap(book, para)}
    secs, cur = _sections(book, para)
    best = latest_summary(book, para)
    out = {"scope": scope, "upto_para": best.upto_para if best else None,
           "chapter": secs[cur].title if cur >= 0 else "", "text": best.text.strip() if best else ""}
    if scope == "series":
        earlier = books.series_recap(book)
        mine = f"This book so far: {out['text']}" if out["text"] else ""
        out["text"] = "\n".join(t for t in (earlier, mine) if t)
    return out
