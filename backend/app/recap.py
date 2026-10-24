"""Recap scopes (#89): the last chapter, this book so far, the series so far. Built only from stored summaries.

Spoiler rule: every scope uses only summaries with `upto_para <= reader_para` (earlier books of a series are fully
read, so their closing summaries are fine). No model call: the stored summaries are "story so far" notes, so the last
chapter's part is cut out of them:
  - uploaded books and most built-in ones mark it ("... The current chapter so far: <note>"): the part after the mark;
  - rolling notes without the mark (offline preprocess): the sentences of the chapter's last note that the note before
    the chapter did not already say (word overlap), else the whole note;
  - the first chapter: its note is all about that chapter.
"""
import re
from typing import Literal

from . import books
from .schema import Book, Section, Summary

Scope = Literal["chapter", "book", "series"]
SCOPES: tuple[str, ...] = ("chapter", "book", "series")

CHAPTER_MARK = "The current chapter so far:"
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


def scoped(book: Book, para: int, scope: Scope) -> dict:
    """{scope, upto_para, chapter, text} for a clamped reader `para`. `upto_para` is the summary the text came from."""
    if scope == "chapter":
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
