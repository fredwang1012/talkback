"""Spoiler-safe hybrid retrieval (brief sections 4 and 6).

HARD INVARIANT (novels): a passage is eligible only if para_end <= reader_para, and the story-so-far summary used
has upto_para <= reader_para. This is enforced here, on the server, and never delegated to the client or the model.
"""
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np
from rank_bm25 import BM25Okapi

from .schema import Book

MAX_PASSAGE_CHARS = 6000
RECENCY_WINDOW = 15
RECENCY_BOOST = 0.05
W_EMBED, W_BM25 = 0.6, 0.4
RECENT_PARAS = 2

_WORD = re.compile(r"\w+", re.UNICODE)


def tokenize(text: str) -> list[str]:
    return _WORD.findall(text.lower())


class Embedder(Protocol):
    def embed_query(self, text: str) -> np.ndarray: ...


@dataclass
class Chunk:
    id: str
    para_start: int
    para_end: int
    text: str
    context_blurb: str = ""


def make_chunks(book: Book, size: int = 3, overlap: int = 1) -> list[Chunk]:
    """Retrieval chunks of `size` paragraphs with `overlap` paragraphs shared between neighbours."""
    paras, step = book.paragraphs, max(1, size - overlap)
    chunks = []
    for start in range(0, len(paras), step):
        group = paras[start:start + size]
        chunks.append(Chunk(id=f"r{len(chunks)}", para_start=group[0].idx, para_end=group[-1].idx,
                            text="\n".join(p.text for p in group)))
        if start + size >= len(paras):
            break
    return chunks


def save_index(dir_: Path, chunks: list[Chunk], embeddings: np.ndarray | None) -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / "chunks.json").write_text(json.dumps([c.__dict__ for c in chunks]), encoding="utf-8")
    if embeddings is not None:
        np.save(dir_ / "embeddings.npy", embeddings.astype(np.float32))


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

    def search(self, query: str, reader_para: int, k: int = 4, *, spoiler_safe: bool = True) -> list[Chunk]:
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


def retrieve(book: Book, index: Index, query: str, reader_para: int, k: int = 4) -> dict:
    """Passages (cap MAX_PASSAGE_CHARS) + story_so_far for the Talk prompt."""
    spoiler_safe = book.kind == "novel"
    reader_para = max(0, min(reader_para, len(book.paragraphs) - 1))
    found = index.search(query, reader_para, k, spoiler_safe=spoiler_safe)
    passages = [{"para_start": c.para_start, "para_end": c.para_end, "text": c.text} for c in found]
    recent = recent_passage(book, reader_para)
    if recent and not any(p["para_start"] <= recent["para_start"] and p["para_end"] >= recent["para_end"] for p in passages):
        passages.append(recent)
    passages.sort(key=lambda p: p["para_start"])
    # hard cap: drop the passages furthest from the reader first, never the recent one
    while sum(len(p["text"]) for p in passages) > MAX_PASSAGE_CHARS and len(passages) > 1:
        victim = max((p for p in passages if p is not recent), key=lambda p: abs(reader_para - p["para_end"]), default=None)
        if victim is None:
            break
        passages.remove(victim)
    total = 0
    for p in passages:  # a single oversized passage is trimmed rather than dropped
        p["text"] = p["text"][: max(0, MAX_PASSAGE_CHARS - total)]
        total += len(p["text"])
    story = latest_summary(book, reader_para) if spoiler_safe else ""
    return {"passages": passages, "story_so_far": story, "mode": index.mode, "reader_para": reader_para}
