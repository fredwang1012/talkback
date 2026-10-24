"""Local meaning-based search (RETRIEVAL_BACKEND=local) for uploads and, at startup, the built-in books.

No embeddings ship in the repo (index/embeddings.npy is gitignored). An upload is published with BM25-only chunks; main._embed_locally then computes multilingual-e5 passage embeddings
here and saves them as index/embeddings.npy, the same format preprocess/index.py writes for the shipped books, so
retrieval.Index.load scores the upload hybrid (cosine + BM25) like a shipped book.
"""
import asyncio
import json
import os
from pathlib import Path

import numpy as np

from .retrieval import Chunk

BATCH = 32   # passages per encode call: progress and delete checks happen between batches


def embed_text(c: Chunk) -> str:
    """What is embedded for a chunk: identical to preprocess/index.py."""
    return (c.context_blurb + "\n" + c.text).strip()


async def embed_chunks(chunks: list[Chunk], embedder, *, batch: int = BATCH, on_progress=None) -> np.ndarray:
    """(len(chunks), dim) float32 passage embeddings. Each batch is encoded in a worker thread (never on the event
    loop); `on_progress(done, total)` runs on the loop after each batch and may raise to stop the job."""
    total = len(chunks)
    parts = []
    for i in range(0, total, batch):
        texts = [embed_text(c) for c in chunks[i:i + batch]]
        parts.append(np.asarray(await asyncio.to_thread(embedder.embed_passages, texts), dtype=np.float32))
        if on_progress is not None:
            on_progress(min(i + batch, total), total)
    return np.vstack(parts) if parts else np.zeros((0, 0), dtype=np.float32)


def load_chunks(index_dir: Path) -> list[Chunk]:
    return [Chunk(**c) for c in json.loads((index_dir / "chunks.json").read_text(encoding="utf-8"))]


def has_embeddings(index_dir: Path, n_chunks: int) -> bool:
    """embeddings.npy exists with one row per chunk (memory-mapped: only the header is read)."""
    path = index_dir / "embeddings.npy"
    if not path.is_file():
        return False
    try:
        return len(np.load(path, mmap_mode="r")) == n_chunks
    except (OSError, ValueError):
        return False


def save_embeddings(index_dir: Path, chunks: list[Chunk], embeddings: np.ndarray) -> bool:
    """Write index_dir/embeddings.npy (atomically) only if chunks.json still holds exactly these chunks. False (nothing
    written) when the folder is gone (book deleted) or the text was re-chunked meanwhile."""
    path = index_dir / "chunks.json"
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if [(c.get("id"), c.get("text")) for c in saved] != [(c.id, c.text) for c in chunks] or len(embeddings) != len(chunks):
        return False
    tmp = index_dir / "embeddings.npy.tmp"
    with open(tmp, "wb") as f:
        np.save(f, np.asarray(embeddings, dtype=np.float32))
    os.replace(tmp, index_dir / "embeddings.npy")
    return True
