"""Title and author for the Add book form (#83): the file's own metadata first, then one cheap LLM call.

`describe(data, ext)` never raises. A PDF's metadata (or a Project Gutenberg "Title:" / "Author:" header in a .txt) is
used when it looks real; otherwise the first pages' text (a PDF's first 3 pages, a text file's opening lines; at most
MAX_CHARS) goes to the fast tier (Gemini Flash, a fraction of a cent) for the title page's title and author. The LLM
runs only when uploads may use one (UPLOAD_ENRICH); its estimated cost is reserved in the upload budget before the call
(no call when the budget cannot cover it), and a global token bucket plus a daily cap bound the calls across every
account. Parsing is bounded too: at most PARSE_CONCURRENCY files at once (Busy when full), PARSE_TIMEOUT_S each.
Nothing is stored: the form shows the answer and the reader can edit it.

Several authors are joined with ", " (#95). Metadata names separated by ";", "and", "&" or commas are split on those
("Last, First" is turned round); names run together with only spaces between them ("Christopher D. Manning Prabhakar
Raghavan Hinrich Schütze") cannot be split by rule, so the model, which sees the title page, is asked for the list.
"""
import asyncio
import logging
import re
from datetime import date

from pydantic import BaseModel, ConfigDict, Field

from ..config import settings
from ..ratelimit import TokenBucket
from . import spend
from .llm import LLM, effective_provider, estimate_usd, make_llm

log = logging.getLogger("talkback.enrich")

PAGES = 3
MAX_CHARS = 6000
MAX_FIELD = 120   # the upload form's limit
TEXT_PREFIX_BYTES = MAX_CHARS * 4   # all that is read of a .txt (the opening lines)
OUT_TOKENS = 200
PARSE_CONCURRENCY = 2   # files parsed at once for this form (a 20 MB PDF is real CPU and memory)
PARSE_TIMEOUT_S = 10.0
DAILY_LLM_CALLS = 300   # across every account; ~$0.0003 each with Gemini Flash
_llm_bucket = TokenBucket(capacity=20, per_seconds=60)   # global (one key): bursts across accounts
_daily = {"day": "", "n": 0}
_parsing = 0


class Busy(Exception):
    """PARSE_CONCURRENCY files are already being read: try again in a moment."""
SYSTEM = """You read the opening pages of a book (title page, copyright page, contents) and give its title and author as printed there.
Title: the book's own title (keep a short subtitle only when it is part of how the book is known). Authors: a list with one entry per author, each name as printed, without "by", in the order printed.
Use an empty string (or an empty list) for anything you cannot find. Never guess from the text's style."""


class Guess(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(description="the book's title, or empty")
    authors: list[str] = Field(description="one entry per author, as printed; empty when not found")


_JUNK_TITLE = re.compile(
    r"^(?:untitled(?:\s+document)?|document\s*\d*|microsoft\s+word\b.*|title|book\d*|no\s+title|unknown|"
    r"new\s+document|print|scan\w*\s*\d*|layout\s*\d*|\W*)$", re.I)
_FILE_NAME = re.compile(r"\.(?:pdf|docx?|odt|rtf|tex|indd|qxd|txt|epub|ps)\b", re.I)
_JUNK_AUTHOR = re.compile(
    r"^(?:administrator|admin|user|owner|unknown|author|default|system|guest|pc|"
    r"adobe\b.*|microsoft\b.*|acrobat\b.*|calibre\b.*|pdf\b.*|.*\bdistiller\b.*|\W*)$", re.I)
_GUTENBERG = {"title": re.compile(r"^\s*Title:\s*(.+)$", re.M), "author": re.compile(r"^\s*Author:\s*(.+)$", re.M)}


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()[:MAX_FIELD].strip()


def junk_title(title: str) -> bool:
    t = clean(title)
    return len(t) < 2 or t.isdigit() or bool(_JUNK_TITLE.match(t)) or bool(_FILE_NAME.search(t))


def junk_author(author: str) -> bool:
    a = clean(author)
    return len(a) < 2 or bool(_JUNK_AUTHOR.match(a))


_AND = re.compile(r"\s+(?:and|&)\s+|\s*&\s*", re.I)
_LEADING_AND = re.compile(r"^(?:and|&)\s+", re.I)
RUN_TOGETHER_WORDS = 4   # an author field this long with no separator may be several names run together


def _turn_round(last: str, first: str) -> str:
    return clean(f"{first} {last}")


def _split_commas(part: str) -> list[str]:
    """"A B, C D, and E F" -> three names; "Austen, Jane" -> "Jane Austen"; "Smith, J., Jones, K." -> two names
    turned round; anything else (e.g. "Martin Luther King, Jr.") is kept as written."""
    bits = [clean(_LEADING_AND.sub("", b.strip())) for b in part.split(",")]
    bits = [b for b in bits if b]
    if len(bits) < 2:
        return bits
    one_word = lambda b: " " not in b  # noqa: E731
    if len(bits) % 2 == 0 and all(one_word(b) for b in bits[0::2]):   # Last, First[, Last, First]
        return [_turn_round(bits[i], bits[i + 1]) for i in range(0, len(bits), 2)]
    if all(not one_word(b) for b in bits):
        return bits
    return [clean(part)]


def split_authors(raw: str) -> list[str]:
    """The names in a metadata / header author field: split on ";", "/", "|", newlines, "and", "&" and commas."""
    out: list[str] = []
    for chunk in re.split(r"[;|/\n]+", raw or ""):
        for part in _AND.split(chunk):
            out.extend(_split_commas(part))
    return out


def join_authors(names: list[str]) -> str:
    """One author field: names cleaned, junk, empties and repeats dropped, joined with ", ", capped at MAX_FIELD."""
    seen: set[str] = set()
    kept: list[str] = []
    for n in names:
        c = clean(n)
        if not c or junk_author(c) or c.lower() in seen:
            continue
        seen.add(c.lower())
        kept.append(c)
    return clean(", ".join(kept))


def run_together(raw: str) -> bool:
    """One long name with no separator: possibly several authors with only spaces between them."""
    names = split_authors(raw)
    return len(names) == 1 and len(names[0].split()) >= RUN_TOGETHER_WORDS


def _pdf_parts(data: bytes) -> tuple[str, str, str]:
    """(metadata title, metadata author, first pages' text) of a PDF; empty strings when it cannot be read."""
    from ..pdfdoc import open_pdf
    try:
        with open_pdf(data) as doc:
            meta = doc.metadata
            parts: list[str] = []
            for i in range(min(PAGES, doc.page_count)):
                parts.append(doc.page(i).plain_text())
                if sum(map(len, parts)) >= MAX_CHARS:
                    break
            return meta["title"], meta["author"], "\n".join(parts)
    except Exception as e:   # a broken or encrypted PDF: the reader types the title
        log.info("describe: could not read the PDF (%s)", type(e).__name__)
        return "", "", ""


def _text_parts(data: bytes) -> tuple[str, str, str]:
    """(Gutenberg header title, author, opening lines) of a text file."""
    head = data[:TEXT_PREFIX_BYTES].decode("utf-8", errors="replace").lstrip("﻿")
    found = {k: (m.group(1) if (m := rx.search(head[:20_000])) else "") for k, rx in _GUTENBERG.items()}
    return found["title"], found["author"], head


def parts(data: bytes, ext: str) -> tuple[str, str, str]:
    """Worker thread: (title, author, text sample <= MAX_CHARS) from the file itself."""
    title, author, text = _pdf_parts(data) if ext == ".pdf" else _text_parts(data)
    text = re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()
    return title, author, text[:MAX_CHARS]


def _public_active() -> bool:
    from .. import public_client
    return public_client.active()


def _take_llm_call() -> bool:
    """One LLM call under the global bucket and the daily cap (checked and counted synchronously)."""
    today = date.today().isoformat()
    if _daily["day"] != today:
        _daily.update(day=today, n=0)
    if _daily["n"] >= DAILY_LLM_CALLS or not _llm_bucket.allow("all"):
        return False
    _daily["n"] += 1
    return True


async def _parse(data: bytes, ext: str) -> tuple[str, str, str]:
    """parts() in a worker thread under the global parse limit and a timeout. On a timeout the thread keeps its slot
    until it really ends (a thread cannot be killed), so slow files can never pile up."""
    global _parsing
    if _parsing >= PARSE_CONCURRENCY:
        raise Busy()
    _parsing += 1   # no await since the check

    def release(_):
        global _parsing
        _parsing -= 1

    fut = asyncio.ensure_future(asyncio.to_thread(parts, data, ext))
    fut.add_done_callback(release)
    try:
        return await asyncio.wait_for(asyncio.shield(fut), PARSE_TIMEOUT_S)
    except asyncio.TimeoutError:
        log.warning("describe: reading the file took over %.0f s; giving up", PARSE_TIMEOUT_S)
        return "", "", ""


async def describe(data: bytes, ext: str, *, llm: LLM | None = None) -> dict:
    """{title, author, source}: source "metadata" (from the file), "llm" (the model filled something) or "none".
    Raises Busy when PARSE_CONCURRENCY files are already being read."""
    meta_title, meta_author, text = await _parse(data, ext)
    title = "" if junk_title(meta_title) else clean(meta_title)
    author = "" if junk_author(meta_author) else join_authors(split_authors(meta_author))
    unsure = bool(author) and run_together(meta_author)   # the model splits the names from the title page
    if title and author and not unsure:
        return {"title": title, "author": author, "source": "metadata"}
    source = "metadata" if title or author else "none"
    if text and llm is None and settings.upload_enrich and _public_active():
        from .. import public_client
        llm = public_client.remote_llm("describe")   # a clone with only a Boson key: the public service's caps apply
    if text and (llm is not None or settings.upload_enrich):
        try:
            if llm is None:
                provider = effective_provider()
                est = estimate_usd(provider, "fast", len(SYSTEM) + len(text), OUT_TOKENS)
                if spend.total_usd() + est > settings.upload_total_cap_usd or not _take_llm_call():
                    return {"title": title, "author": author, "source": source}
                spend.add("describe", provider, est)   # reserved before the call; any overage is added after it
                llm = make_llm()
                llm.on_usage = lambda prov, model, n_in, n_out, usd: spend.add("describe", prov, max(0.0, usd - est))
            guess = await llm.complete_json(SYSTEM, text, Guess, "fast", max_tokens=OUT_TOKENS)
            g_title, g_author = clean(guess.title), join_authors(guess.authors)
            if not title and g_title and not junk_title(g_title):
                title, source = g_title, "llm"
            if (not author or unsure) and g_author:
                author, source = g_author, "llm"
        except Exception as e:   # the form keeps the file name as the title
            log.warning("describe by LLM failed (%s)", type(e).__name__)
    return {"title": title, "author": author, "source": source}
