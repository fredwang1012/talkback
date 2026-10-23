"""Questions in other languages (#12): the reader is answered in their own language, and the (English) book is searched
with an English translation of the question, so BM25 and the English summaries still find the right passages.

The language guess mirrors the reader's badge (frontend/src/talk/language.ts): a non-Latin script, or the common words
of a language. It is deliberately cautious: a question only counts as non-English when its script says so or at least
two of its common words belong to that language and outnumber the English ones, so an English question is never told
to be answered in Spanish. Translation uses Gemini on Vertex when GCP_PROJECT is set; without it (or on any failure) the
original question is searched, which the multilingual embeddings still partly match.
"""
import asyncio
import logging
import re
from collections import OrderedDict

from .config import settings

_SCRIPTS = [
    ("ja", "Japanese", re.compile(r"[぀-ヿ]")),
    ("ko", "Korean", re.compile(r"[가-힯]")),
    ("zh", "Chinese", re.compile(r"[一-鿿]")),
    ("ru", "Russian", re.compile(r"[Ѐ-ӿ]")),
    ("ar", "Arabic", re.compile(r"[؀-ۿ]")),
    ("hi", "Hindi", re.compile(r"[ऀ-ॿ]")),
    ("he", "Hebrew", re.compile(r"[֐-׿]")),
    ("el", "Greek", re.compile(r"[Ͱ-Ͽ]")),
    ("th", "Thai", re.compile(r"[฀-๿]")),
]
_STOPWORDS = [
    ("en", "English", {"the", "is", "and", "what", "who", "why", "how", "does", "did", "this", "that", "of", "to", "he", "she",
                       "do", "you", "was", "are", "it"}),
    ("es", "Spanish", {"el", "la", "los", "las", "que", "quién", "quien", "por", "qué", "es", "una", "un", "del", "con", "cómo",
                       "como", "y", "se", "para", "está"}),
    ("fr", "French", {"le", "la", "les", "est", "que", "qui", "pourquoi", "quoi", "une", "un", "des", "du", "et", "comment", "pas",
                      "ce", "cette", "dans", "sur", "elle"}),
    ("de", "German", {"der", "die", "das", "ist", "und", "was", "wer", "warum", "wie", "nicht", "ein", "eine", "mit", "ich", "sie",
                      "er", "zu", "den", "dem", "auf"}),
    ("pt", "Portuguese", {"o", "os", "as", "que", "quem", "por", "porque", "é", "uma", "um", "do", "da", "com", "como", "não", "se",
                          "para", "está", "ele", "ela"}),
    ("it", "Italian", {"il", "lo", "gli", "che", "chi", "perché", "è", "una", "un", "del", "della", "con", "come", "non", "si",
                       "per", "sono", "questo", "lei", "lui"}),
]
_WORDS = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)?")
MIN_WORDS = 2            # common words a Latin-script language needs before it counts


def detect_language(text: str) -> tuple[str, str] | None:
    """(code, English name) of the language a question is written in, or None when there is too little to go on."""
    t = text.strip()
    if len(t) < 2:
        return None
    for code, name, script in _SCRIPTS:
        if script.search(t):
            return code, name
    words = [w.lower() for w in _WORDS.findall(t)]
    if not words:
        return None
    scores = [(sum(w in common for w in words), code, name) for code, name, common in _STOPWORDS]
    english = scores[0][0]
    best = max(scores[1:], key=lambda s: s[0])
    if best[0] >= MIN_WORDS and best[0] > english:
        return best[1], best[2]
    return ("en", "English") if english or len(words) >= 3 else None


def foreign_language(text: str) -> str | None:
    """The English name of the question's language when it is not English (and clearly detected), else None."""
    lang = detect_language(text)
    return lang[1] if lang and lang[0] != "en" else None


# ---- translation for searching ----

TRANSLATE_SYSTEM = ("Translate the reader's question about a book into English, for searching the book's English text. "
                    "Keep the names of people, places and things as an English edition of the book would spell them. "
                    "Reply with the English question only, nothing else.")
TRANSLATE_TIMEOUT_S = 4.0  # the answer waits for this: past it the original question is searched
MAX_CACHE = 256
MAX_QUERY_CHARS = 500
_cache: OrderedDict[str, str] = OrderedDict()
_client = None


class Unavailable(Exception):
    """No translation backend is configured (no GCP_PROJECT)."""


async def translate(text: str) -> str:
    """English translation of `text` with Gemini on Vertex. Raises on any failure (the caller falls back)."""
    if not settings.gcp_project:
        raise Unavailable("no GCP_PROJECT")
    from google import genai
    from google.genai import types

    from .gcp import usage
    global _client
    if _client is None:
        _client = genai.Client(vertexai=True, project=settings.gcp_project, location=settings.gemini_region())
    config = types.GenerateContentConfig(system_instruction=TRANSLATE_SYSTEM, temperature=0, max_output_tokens=200,
                                         thinking_config=types.ThinkingConfig(thinking_budget=0))
    try:
        resp = await _client.aio.models.generate_content(model=settings.gemini_model, contents=text, config=config)
    except Exception as e:
        if "thinking" not in str(e).lower():
            raise
        # models that always think reject a zero budget: let it think, with room for the hidden thoughts
        config = types.GenerateContentConfig(system_instruction=TRANSLATE_SYSTEM, temperature=0, max_output_tokens=2048)
        resp = await _client.aio.models.generate_content(model=settings.gemini_model, contents=text, config=config)
    meta = getattr(resp, "usage_metadata", None)
    if meta is not None:
        usage.record("llm", settings.gemini_model, prompt_tokens=meta.prompt_token_count or 0,
                     output_tokens=meta.candidates_token_count or 0)
    return resp.text or ""


async def search_query(query: str) -> str:
    """The query to search the English book with: the question itself, or its English translation when it is written in
    another language. Translations are cached; a failure or timeout searches the original question."""
    q = query.strip()
    if not q or foreign_language(q) is None:
        return query
    if q in _cache:
        _cache.move_to_end(q)
        return _cache[q]
    try:
        english = await asyncio.wait_for(translate(q[:MAX_QUERY_CHARS]), TRANSLATE_TIMEOUT_S)
    except Unavailable:
        return query
    except Exception as e:  # never fail the question over its translation
        logging.getLogger(__name__).warning("question translation failed: %s", type(e).__name__)
        return query
    english = " ".join(english.split()).strip("\"'“”")
    if not english:
        return query
    _cache[q] = english
    while len(_cache) > MAX_CACHE:
        _cache.popitem(last=False)
    return english
