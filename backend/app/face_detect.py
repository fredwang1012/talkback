"""The main face's bounding box in a photo, for the head-and-shoulders crop of face uploads (#109).

`detect(image, ext)` never raises: it returns {x, y, w, h} (fractions of the image, clamped inside it) or None (no
face, a bad answer, a failed call, or no call allowed). One Gemini Flash call on Vertex (a fraction of a cent) with
structured JSON output. Guarded like the Add book describe (#83): its estimated cost is reserved in the upload budget
before the call (no call when the budget cannot cover it), and a global token bucket plus a daily cap bound the calls
across every account; the endpoint adds a per-caller bucket. Nothing is stored and the image is never logged.
"""
import json
import logging
from datetime import date

from .config import settings
from .enrich import spend
from .enrich.llm import price_usd
from .gcp import usage as usage_log
from .ratelimit import TokenBucket

log = logging.getLogger("talkback.faces")

MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}
IMAGE_TOKENS = 1300   # Gemini bills an image as ~258 tokens per 768 px tile; a 5 MB photo is a few tiles
OUT_TOKENS = 100
DAILY_LLM_CALLS = 300   # across every account; ~$0.0006 each with Gemini Flash
_llm_bucket = TokenBucket(capacity=20, per_seconds=60)   # global (one key): bursts across accounts
_daily = {"day": "", "n": 0}

PROMPT = ("Find the main person's face in this photo (the largest, most central one). Give its bounding box, "
          "from the top of the hair to the chin and ear to ear, as fractions of the image width and height: "
          "x and y of the top-left corner, w and h the size, all between 0 and 1. found is false when there is no face.")
SCHEMA = {
    "type": "object",
    "properties": {"found": {"type": "boolean"}, "x": {"type": "number"}, "y": {"type": "number"},
                   "w": {"type": "number"}, "h": {"type": "number"}},
    "required": ["found", "x", "y", "w", "h"],
}


def clamp_box(x: float, y: float, w: float, h: float) -> dict | None:
    """The box clamped inside the unit square, or None when nothing of it is left. A box clearly on Gemini's native
    0..1000 scale is read as such."""
    if max(x + w, y + h) > 2:
        x, y, w, h = x / 1000, y / 1000, w / 1000, h / 1000
    if w <= 0 or h <= 0:
        return None
    x0, y0 = max(0.0, x), max(0.0, y)
    x1, y1 = min(1.0, x + w), min(1.0, y + h)
    if x1 - x0 < 0.01 or y1 - y0 < 0.01:
        return None
    return {"x": round(x0, 4), "y": round(y0, 4), "w": round(x1 - x0, 4), "h": round(y1 - y0, 4)}


def parse(text: str) -> dict | None:
    try:
        d = json.loads(text)
        if not isinstance(d, dict) or not d.get("found"):
            return None
        vals = [d[k] for k in "xywh"]
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in vals):
            return None
        return clamp_box(*map(float, vals))
    except (ValueError, KeyError):
        return None


def _take_llm_call() -> bool:
    """One call under the global bucket and the daily cap (checked and counted synchronously)."""
    today = date.today().isoformat()
    if _daily["day"] != today:
        _daily.update(day=today, n=0)
    if _daily["n"] >= DAILY_LLM_CALLS or not _llm_bucket.allow("all"):
        return False
    _daily["n"] += 1
    return True


async def _gemini(image: bytes, mime: str) -> str:
    """One structured-output Gemini Flash call on Vertex; returns the JSON text. Usage is metered and any cost over
    the reserved estimate is added to the upload spend."""
    from google import genai
    from google.genai import types
    client = genai.Client(vertexai=True, project=settings.gcp_project, location=settings.gcp_region)
    config = types.GenerateContentConfig(
        max_output_tokens=1024, response_mime_type="application/json", response_json_schema=SCHEMA,
        thinking_config=types.ThinkingConfig(thinking_budget=0),
    )
    resp = await client.aio.models.generate_content(
        model=settings.gemini_model, contents=[types.Part.from_bytes(data=image, mime_type=mime), PROMPT], config=config)
    meta = getattr(resp, "usage_metadata", None)
    if meta is not None:
        n_in, n_out = meta.prompt_token_count or 0, meta.candidates_token_count or 0
        usage_log.record("llm", settings.gemini_model, prompt_tokens=n_in, output_tokens=n_out)
        over = price_usd("gemini", "fast", n_in, n_out) - estimate_usd()
        if over > 0:
            spend.add("face-detect", "gemini", over)
    return resp.text or ""


def estimate_usd() -> float:
    return price_usd("gemini", "fast", IMAGE_TOKENS + len(PROMPT) // 3, OUT_TOKENS)


async def detect(image: bytes, ext: str) -> dict | None:
    """{x, y, w, h} of the main face as fractions of the image, or None. A clone with no GCP project of its own asks
    the public processing service (its own caps and rate limits apply there)."""
    if not settings.gcp_project:
        from . import public_client
        if public_client.active():
            return await public_client.detect_face(image, ext)
        return None
    est = estimate_usd()
    if spend.total_usd() + est > settings.upload_total_cap_usd or not _take_llm_call():
        return None
    spend.add("face-detect", "gemini", est)   # reserved before the call
    try:
        return parse(await _gemini(image, MIME.get(ext, "image/jpeg")))
    except Exception as e:   # the app falls back to a centred square
        log.warning("face detection failed (%s)", type(e).__name__)
        return None
