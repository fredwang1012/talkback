"""Spoiler-safe hybrid retrieval (brief sections 4 and 6).

HARD INVARIANT (novels): a passage is eligible only if para_end <= reader_para, and the story-so-far summary used
has upto_para <= reader_para. This is enforced here, on the server, and never delegated to the client or the model.
Series: passages from books BEFORE this one in the same series are always eligible (the reader has finished them).
"""
import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np
from rank_bm25 import BM25Okapi

from .schema import Book

DEFAULT_K = 8                  # passages per Gemini answer (/api/answer)
MAX_PASSAGE_CHARS = 12000      # hard cap on passage text per Gemini answer
REALTIME_K = 4                 # passages per Boson realtime session (/api/retrieve): its instructions stay short
REALTIME_MAX_PASSAGE_CHARS = 4500
CHUNK_TARGET, CHUNK_MIN, CHUNK_MAX = 1400, 900, 2000   # retrieval chunk size in characters
CHUNK_OVERLAP_CHARS = 250      # neighbouring chunks share about one trailing paragraph (up to this many chars)
RECENCY_WINDOW = 15
RECENCY_BOOST = 0.05
W_EMBED, W_BM25 = 0.6, 0.4
RECENT_PARAS = 2

_WORD = re.compile(r"\w+", re.UNICODE)
# Chinese / Japanese are written without spaces (#117): a run of such characters is one \w+ "word" (a whole clause),
# so it is indexed as overlapping character pairs instead, the usual BM25 tokens for CJK text (a lone character stays).
_CJK_RUN = re.compile(r"[぀-ヿ㐀-䶿一-鿿豈-﫿]+")


def _cjk_tokens(word: str) -> list[str]:
    out: list[str] = []
    pos = 0
    for m in _CJK_RUN.finditer(word):
        if m.start() > pos:
            out.append(word[pos:m.start()])
        run = m.group()
        out += [run] if len(run) == 1 else [run[i:i + 2] for i in range(len(run) - 1)]
        pos = m.end()
    if pos < len(word):
        out.append(word[pos:])
    return out


def tokenize(text: str) -> list[str]:
    words = _WORD.findall(text.lower())
    if not _CJK_RUN.search(text):   # no CJK: the tokens are what they always were
        return words
    return [t for w in words for t in (_cjk_tokens(w) if _CJK_RUN.search(w) else [w])]


class Embedder(Protocol):
    def embed_query(self, text: str) -> np.ndarray: ...


@dataclass
class Chunk:
    id: str
    para_start: int
    para_end: int
    text: str
    context_blurb: str = ""
    book_id: str = ""     # set only for passages from an earlier book of the series ("" = the book being read)
    book_title: str = ""


def make_chunks(book: Book, target: int = CHUNK_TARGET, min_chars: int = CHUNK_MIN, max_chars: int = CHUNK_MAX,
                overlap_chars: int = CHUNK_OVERLAP_CHARS) -> list[Chunk]:
    """Paragraph-aligned chunks of about `target` chars (never above `max_chars` unless one paragraph alone is longer).

    A chunk closes once it reaches `min_chars` and adding the next paragraph would overshoot `target`, or would exceed
    `max_chars`. Neighbours overlap by the trailing paragraph(s) totalling at most `overlap_chars` (at least one
    paragraph when that paragraph alone fits within the limit; the next chunk always advances).
    """
    paras = book.paragraphs
    n = len(paras)
    lens = [len(p.text) + 1 for p in paras]  # +1 for the joining newline
    chunks: list[Chunk] = []
    start, fresh = 0, 0  # `fresh`: first paragraph not already in the previous chunk (each chunk must add one)
    while start < n:
        end, size = start, lens[start]  # inclusive end
        while end + 1 < n:
            nxt = lens[end + 1]
            if size + nxt > max_chars or (end >= fresh and size >= min_chars and size + nxt > target):
                break
            end += 1
            size += nxt
        chunks.append(Chunk(id=f"r{len(chunks)}", para_start=paras[start].idx, para_end=paras[end].idx,
                            text="\n".join(p.text for p in paras[start:end + 1])))
        if end + 1 >= n:
            break
        # next chunk starts at the earliest paragraph (>= start+1) such that the overlap stays within overlap_chars,
        # but always keeps at least the last paragraph as overlap
        nstart, shared = end, lens[end]
        while nstart - 1 > start and shared + lens[nstart - 1] <= overlap_chars:
            nstart -= 1
            shared += lens[nstart]
        while nstart <= end and sum(lens[nstart:end + 2]) > max_chars:  # overlap must leave room for the next paragraph
            nstart += 1
        start, fresh = max(nstart, start + 1), end + 1
    return chunks


def save_index(dir_: Path, chunks: list[Chunk], embeddings: np.ndarray | None) -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / "chunks.json").write_text(json.dumps([c.__dict__ for c in chunks]), encoding="utf-8")
    if embeddings is not None:
        np.save(dir_ / "embeddings.npy", embeddings.astype(np.float32))
    else:   # new chunks without vectors: older vectors (if any) belong to other chunks
        (dir_ / "embeddings.npy").unlink(missing_ok=True)


class Index:
    def __init__(self, chunks: list[Chunk], embeddings: np.ndarray | None = None, embedder: Embedder | None = None):
        self.chunks = chunks
        self.embeddings = embeddings
        self.embedder = embedder
        self._bm25 = BM25Okapi([tokenize(c.context_blurb + " " + c.text) for c in chunks]) if chunks else None

    @classmethod
    def load(cls, dir_: Path, embedder: Embedder | None = None) -> "Index":
        chunks = [Chunk(**c) for c in json.loads((dir_ / "chunks.json").read_text(encoding="utf-8"))]
        emb_path = dir_ / "embeddings.npy"
        emb = np.load(emb_path) if emb_path.exists() else None
        if emb is not None and len(emb) != len(chunks):   # vectors from other chunks: BM25 until they are rebuilt
            emb = None
        return cls(chunks, emb, embedder if emb is not None else None)

    @property
    def mode(self) -> str:
        return "hybrid" if self.embeddings is not None and self.embedder is not None else "bm25"

    def scores(self, query: str) -> np.ndarray:
        """Hybrid score for every chunk (before eligibility filtering). Min-max normalised BM25 + cosine."""
        n = len(self.chunks)
        bm = np.asarray(self._bm25.get_scores(tokenize(query)), dtype=float) if self._bm25 and query.strip() else np.zeros(n)
        lo, hi = (bm.min(), bm.max()) if n else (0, 0)
        bm_norm = (bm - lo) / (hi - lo) if hi > lo else np.zeros(n)
        if self.mode == "bm25" or not query.strip():
            return bm_norm
        q = self.embedder.embed_query(query)
        cos = self.embeddings @ (q / (np.linalg.norm(q) or 1.0))
        return W_EMBED * cos + W_BM25 * bm_norm

    def search(self, query: str, reader_para: int, k: int = DEFAULT_K, *, spoiler_safe: bool = True) -> list[Chunk]:
        """Top-k chunks, in book order. With spoiler_safe, only chunks fully at or before reader_para are eligible."""
        eligible = [i for i, c in enumerate(self.chunks) if (c.para_end <= reader_para if spoiler_safe else True)]
        if not eligible:
            return []
        s = self.scores(query)
        scored = []
        for i in eligible:
            c = self.chunks[i]
            gap = reader_para - c.para_end if spoiler_safe else abs(reader_para - (c.para_start + c.para_end) / 2)
            boost = RECENCY_BOOST if gap <= RECENCY_WINDOW else 0.0
            scored.append((s[i] + boost, i))
        scored.sort(reverse=True)
        return sorted((self.chunks[i] for _, i in scored[:k]), key=lambda c: c.para_start)


def recent_passage(book: Book, reader_para: int) -> dict | None:
    """The last RECENT_PARAS paragraphs up to and including reader_para ("what just happened")."""
    if not book.paragraphs:
        return None
    end = min(reader_para, len(book.paragraphs) - 1)
    start = max(0, end - RECENT_PARAS + 1)
    return {"para_start": start, "para_end": end, "text": "\n".join(p.text for p in book.paragraphs[start:end + 1])}


def latest_summary(book: Book, reader_para: int) -> str:
    """Largest summary written only from text at or before reader_para."""
    ok = [s for s in book.summaries if s.upto_para <= reader_para]
    return max(ok, key=lambda s: s.upto_para).text if ok else ""


def is_eligible(c: Chunk, book: Book, reader_para: int, earlier: frozenset[str]) -> bool:
    """Spoiler rule: anything from an earlier book of the series, or this book up to reader_para."""
    if c.book_id and c.book_id != book.id:
        return c.book_id in earlier
    return c.para_end <= reader_para


def final_summary(book: Book) -> str:
    """The book's last (whole-book) summary: only used in full-book (spoilers on) mode."""
    return max(book.summaries, key=lambda s: s.upto_para).text if book.summaries else ""


def _finish(book: Book, index, found: list[Chunk], reader_para: int, spoiler_safe: bool,
            earlier: frozenset[str] = frozenset(), recap: str = "", full_book: bool = False,
            max_chars: int = MAX_PASSAGE_CHARS) -> dict:
    """Shared post-search step: recent passage, char cap (`max_chars`), story summary (with the earlier books' recap)."""
    if spoiler_safe:  # backstop: enforce the spoiler invariant whatever the index returned
        found = [c for c in found if is_eligible(c, book, reader_para, earlier)]
    else:  # textbook, or full-book mode (novel, spoilers on): the whole book, plus earlier books if any
        found = [c for c in found if not c.book_id or c.book_id == book.id or (full_book and c.book_id in earlier)]
    passages = []
    for c in found:
        p = {"para_start": c.para_start, "para_end": c.para_end, "text": c.text}
        if c.book_id and c.book_id != book.id:
            p["book"] = c.book_title or c.book_id
        passages.append(p)
    recent = recent_passage(book, reader_para)
    if recent and not any("book" not in p and p["para_start"] <= recent["para_start"] and p["para_end"] >= recent["para_end"]
                          for p in passages):
        passages.append(recent)
    passages.sort(key=lambda p: ("book" not in p, p["para_start"]))  # earlier books first, then this book in order

    def distance(p: dict) -> float:
        return float("inf") if "book" in p else abs(reader_para - p["para_end"])

    # hard cap: drop the passages furthest from the reader first (earlier books count as furthest), never the recent one
    while sum(len(p["text"]) for p in passages) > max_chars and len(passages) > 1:
        victim = max((p for p in passages if p is not recent), key=distance, default=None)
        if victim is None:
            break
        passages.remove(victim)
    total = 0
    for p in passages:  # a single oversized passage is trimmed rather than dropped
        p["text"] = p["text"][: max(0, max_chars - total)]
        total += len(p["text"])
    story = latest_summary(book, reader_para) if spoiler_safe else ""
    if spoiler_safe and recap:
        story = f"Earlier books:\n{recap}\n\nThis book so far:\n{story or '(just started)'}"
    elif full_book:  # whole-book note (the last summary) plus the earlier books' recap
        whole = final_summary(book)
        parts = ([f"Earlier books:\n{recap}"] if recap else []) + ([f"Whole book:\n{whole}"] if whole else [])
        story = "\n\n".join(parts)
    return {"passages": passages, "story_so_far": story, "mode": index.mode, "reader_para": reader_para}


def _clamp(book: Book, reader_para: int, spoilers: bool = False) -> tuple[bool, int]:
    """(spoiler_safe, clamped para). Full-book mode (`spoilers`) only applies to novels."""
    return book.kind == "novel" and not spoilers, max(0, min(reader_para, len(book.paragraphs) - 1))


def retrieve(book: Book, index: Index, query: str, reader_para: int, k: int = DEFAULT_K, *, spoilers: bool = False,
             max_chars: int = MAX_PASSAGE_CHARS) -> dict:
    """Passages (cap `max_chars`, default MAX_PASSAGE_CHARS) + story_so_far for the Talk prompt."""
    spoiler_safe, reader_para = _clamp(book, reader_para, spoilers)
    found = index.search(query, reader_para, k, spoiler_safe=spoiler_safe)
    return _finish(book, index, found, reader_para, spoiler_safe, full_book=book.kind == "novel" and spoilers, max_chars=max_chars)


async def aretrieve(book: Book, index, query: str, reader_para: int, k: int = DEFAULT_K, *,
                    earlier: frozenset[str] = frozenset(), recap: str = "",
                    spoilers: bool = False, max_chars: int = MAX_PASSAGE_CHARS) -> dict:
    """Async `retrieve`: same output; awaits index.asearch when present, else runs the sync search in a thread.

    `earlier`: ids of the series books before this one (fully eligible); `recap`: their closing summaries.
    `spoilers`: full-book mode (novels only): the whole book and the earlier books are eligible.
    """
    spoiler_safe, reader_para = _clamp(book, reader_para, spoilers)
    if hasattr(index, "asearch"):
        found = await index.asearch(query, reader_para, k, spoiler_safe=spoiler_safe)
    else:
        found = await asyncio.to_thread(index.search, query, reader_para, k, spoiler_safe=spoiler_safe)
    return _finish(book, index, found, reader_para, spoiler_safe, earlier, recap,
                   full_book=book.kind == "novel" and spoilers, max_chars=max_chars)
