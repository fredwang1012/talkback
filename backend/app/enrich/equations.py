"""LaTeX and a spoken reading for the display equations of an uploaded textbook (#41), from their cropped pictures.

PDFs store drawn glyphs, not LaTeX, so the text alone cannot give the equation back. Each picture (see
ingest_upload.render_equations) goes to Gemini Flash on Vertex, BATCH pictures per call, for its LaTeX (rendered by
KaTeX in the reader) and a short plain-English reading (what the narrator says). Gemini only: the pictures are image
parts of the request, and the Claude credit is gone (see CLAUDE.md).

Cost controls as for novel enrichment: an estimate before starting (refused above the per-book cap or when the upload
budget cannot cover it), real spend added to data/usage/upload_spend.json, and a stop once the job passes the per-book
cap by BOOK_CAP_MARGIN or the budget is used. A failed batch keeps its equations as pictures. Never raises.
"""
import asyncio
import logging

from pydantic import BaseModel, ConfigDict, Field

from ..config import settings
from . import spend
from .llm import LLM, SpendLimit, estimate_usd, make_llm
from .pipeline import BOOK_CAP_MARGIN

log = logging.getLogger("talkback.enrich")

BATCH = 8
IMAGE_TOKENS = 260          # Gemini bills a small picture at ~258 tokens
OUT_TOKENS_PER = 120        # LaTeX plus a spoken line
HINT_CHARS = 160            # of the extracted (garbled) text sent along as a hint
MAX_LATEX = 1500
MAX_SPOKEN = 240
ESTIMATE_MARGIN = 1.2

SYSTEM = """You transcribe typeset mathematics from pictures cropped out of a textbook PDF.
For each picture, by its id:
- latex: the equation in LaTeX math mode for KaTeX, without $ or \\[ delimiters and without \\begin{equation}. Several lines: use \\begin{aligned} ... \\end{aligned}. Leave out the equation number such as (3.4).
- spoken: how a teacher reads it aloud, in plain English words only (no LaTeX, no symbols), at most 35 words, e.g. "y hat equals beta zero hat plus beta one hat times x".
Pictures whose id starts with "m" are formulas from inside a sentence: their spoken reading replaces the formula in that sentence, so keep it short (at most 15 words), e.g. "beta one hat".
The extracted text given with each picture is often garbled; trust the picture.
If a picture is not an equation (prose, a table, a figure, blank), give an empty latex and an empty spoken."""


class Reading(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    latex: str = Field(description="LaTeX math, or empty when the picture is no equation")
    spoken: str = Field(description="plain-English reading, or empty")


class Readings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[Reading]


def available() -> bool:
    """Gemini on Vertex is configured and uploads may use an LLM."""
    return settings.upload_enrich and bool(settings.gcp_project)


def estimate(n: int) -> float:
    calls = -(-n // BATCH)
    in_chars = (n * (IMAGE_TOKENS * 3.5 + HINT_CHARS + 40)) + calls * len(SYSTEM)
    return round(estimate_usd("gemini", "fast", in_chars, n * OUT_TOKENS_PER) * ESTIMATE_MARGIN, 4)


def check_caps(est: float) -> str | None:
    """None when the job may start; else why not (logged, the equations stay pictures)."""
    if est > settings.upload_book_cap_usd:
        return "estimate above the per-book cap"
    if spend.total_usd() + est > settings.upload_total_cap_usd:
        return "upload budget used"
    return None


def _clean(r: Reading) -> dict | None:
    latex = r.latex.strip().strip("$").strip()
    for wrap in (("\\[", "\\]"), ("\\(", "\\)")):
        if latex.startswith(wrap[0]) and latex.endswith(wrap[1]):
            latex = latex[2:-2].strip()
    spoken = " ".join(r.spoken.split())
    if not latex or len(latex) > MAX_LATEX:
        return None
    return {"latex": latex, "spoken": spoken[:MAX_SPOKEN] or None}


def _parts(batch: list[tuple[str, bytes, str]]) -> list:
    from google.genai import types
    out: list = []
    for eq_id, png, hint in batch:
        out.append(types.Part.from_text(text=f"Picture {eq_id} (extracted text: {hint[:HINT_CHARS]!r}):"))
        out.append(types.Part.from_bytes(data=png, mime_type="image/png"))
    return out


def fitting(n: int, spent: float = 0.0) -> int:
    """How many of n pictures fit under the per-book cap after `spent`."""
    while n and estimate(n) + spent > settings.upload_book_cap_usd:
        n = n * 9 // 10
    return n


async def transcribe(book_id: str, crops: dict[str, tuple[bytes, str]], *, llm: LLM | None = None,
                     parts=_parts, fit: bool = False) -> dict[str, dict]:
    """{equation id: {latex, spoken}} for the pictures Gemini could read. `crops` is {id: (PNG, extracted text)}.
    With `fit` (inline formulas, after the display equations on the same `llm`), only the first pictures that fit
    under the per-book cap are sent instead of none."""
    if not crops:
        return {}
    spent = llm.usd if llm is not None else 0.0
    if fit:
        crops = dict(list(crops.items())[:fitting(len(crops), spent)])
        if not crops:
            return {}
    est = estimate(len(crops))
    reason = check_caps(est + spent)
    if reason:
        log.info("equations of %s not transcribed: %s ($%.2f estimated)", book_id, reason, est)
        return {}
    llm = llm or make_llm("gemini")
    cap = settings.upload_book_cap_usd * BOOK_CAP_MARGIN

    def on_usage(provider: str, model: str, n_in: int, n_out: int, usd: float) -> None:
        total = spend.add(book_id, provider, usd, flush_now=False)
        if total >= settings.upload_total_cap_usd:
            raise SpendLimit("upload budget used")
        if llm.usd > cap:
            raise SpendLimit("book cap")

    llm.on_usage = on_usage
    items = [(k, png, hint) for k, (png, hint) in crops.items()]
    batches = [items[i:i + BATCH] for i in range(0, len(items), BATCH)]
    found: dict[str, dict] = {}

    async def one(batch: list[tuple[str, bytes, str]]) -> None:
        ids = {k for k, _png, _hint in batch}
        try:
            res = await llm.complete_json(SYSTEM, parts(batch), Readings, "fast",
                                          max_tokens=OUT_TOKENS_PER * len(batch) * 3)
        except SpendLimit:
            raise
        except Exception as e:  # this batch stays pictures
            log.warning("equation batch failed (%s)", type(e).__name__)
            return
        for r in res.items:
            if r.id in ids and (clean := _clean(r)):
                found[r.id] = clean

    tasks = [asyncio.ensure_future(one(b)) for b in batches]
    try:
        await asyncio.gather(*tasks)
    except SpendLimit as e:
        log.warning("equations of %s stopped: %s", book_id, e)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await asyncio.shield(asyncio.to_thread(spend.flush))
    log.info("equations of %s: %d of %d read, $%.4f in %d calls", book_id, len(found), len(crops), llm.usd, llm.calls)
    return found
