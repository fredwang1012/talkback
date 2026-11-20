"""Questions in other languages (#12): the reader is answered in their own language, and the (English) book is searched
with an English translation of the question, so BM25 and the English summaries still find the right passages.

The language guess mirrors the reader's badge (frontend/src/talk/language.ts): a non-Latin script, or the common words
of a language. It is deliberately cautious: a question only counts as non-English when its script says so or at least
two of its common words belong to that language and outnumber the English ones, so an English question is never told
to be answered in Spanish. Translation uses Gemini on Vertex when GCP_PROJECT is set, else the public processing service
when the clone uses it (PUBLIC_API_URL); without either (or on any failure) the original question is searched, which the multilingual embeddings still partly match.

A book in another language (#117, Book.language) is searched in its own language instead: see search_query.
"""
import asyncio
import logging
import re
from collections import OrderedDict

from . import booklang, pacer
from .config import settings

RETRY_ATTEMPTS = 2     # translation is on the question's critical path: one retry, a short wait
RETRY_BASE_S = 0.3

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


def clear_language(text: str) -> tuple[str, str] | None:
    """(code, English name) only when the question clearly is in that language, English included: its script, or at
    least two common words of one language and more than of any other (the reader's clearLanguage). Used for a book in
    another language (#117), whose answers otherwise default to the book's language."""
    t = text.strip()
    if len(t) < 2:
        return None
    for code, name, script in _SCRIPTS:
        if script.search(t):
            return code, name
    words = [w.lower() for w in _WORDS.findall(t)]
    scores = sorted(((sum(w in common for w in words), code, name) for code, name, common in _STOPWORDS),
                    key=lambda s: -s[0])
    best, second = scores[0], scores[1]
    return (best[1], best[2]) if best[0] >= MIN_WORDS and best[0] > second[0] else None


ISO_639_3 = {"en": "eng", "es": "spa", "fr": "fra", "de": "deu", "pt": "por", "it": "ita", "ja": "jpn", "ko": "kor",
             "zh": "zho", "ru": "rus", "ar": "ara", "hi": "hin", "he": "heb", "el": "ell", "th": "tha"}


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


def translate_system(language: str) -> str:
    """TRANSLATE_SYSTEM for a book written in `language` (#117): the question is searched in the book's language."""
    return (f"Translate the reader's question about a book into {language}, for searching the book's {language} text. "
            "Keep the names of people, places and things as the book spells them. "
            f"Reply with the {language} question only, nothing else.")


async def translate(text: str, language: str | None = None) -> str:
    """English translation of `text` with Gemini on Vertex, or through the public processing service on a clone with
    no LLM of its own (public_client.active()). Raises on any failure (the caller falls back). `language` (an English
    language name): translate into that language instead (Gemini only; the service translates into English)."""
    if not settings.gcp_project:
        from . import public_client
        if public_client.active() and language is None:
            return await public_client.translate(text)
        raise Unavailable("no GCP_PROJECT")
    from google import genai
    from google.genai import types

    from .gcp import usage
    global _client
    if _client is None:
        _client = genai.Client(vertexai=True, project=settings.gcp_project, location=settings.gemini_region())
    system = TRANSLATE_SYSTEM if language is None else translate_system(language)
    config = types.GenerateContentConfig(system_instruction=system, temperature=0, max_output_tokens=200,
                                         thinking_config=types.ThinkingConfig(thinking_budget=0))

    async def once():
        nonlocal config
        try:
            return await _client.aio.models.generate_content(model=settings.gemini_model, contents=text, config=config)
        except Exception as e:
            if "thinking" not in str(e).lower():
                raise
            # models that always think reject a zero budget: let it think, with room for the hidden thoughts
            config = types.GenerateContentConfig(system_instruction=system, temperature=0, max_output_tokens=2048)
        return await _client.aio.models.generate_content(model=settings.gemini_model, contents=text, config=config)

    # one quick retry of a network blip, short enough to fit the caller's TRANSLATE_TIMEOUT_S
    resp = await pacer.shared.call(once, priority="interactive", what="translation", attempts=RETRY_ATTEMPTS,
                                   base=RETRY_BASE_S, cap=RETRY_BASE_S)
    meta = getattr(resp, "usage_metadata", None)
    if meta is not None:
        usage.record("llm", settings.gemini_model, prompt_tokens=meta.prompt_token_count or 0,
                     output_tokens=meta.candidates_token_count or 0)
    return resp.text or ""


async def search_query(query: str, book_language: str | None = None) -> str:
    """The query to search the English book with: the question itself, or its English translation when it is written in
    another language. Translations are cached; a failure or timeout searches the original question.

    A book in another language (`book_language`, ISO 639-3, #117) is searched in its own language: a question clearly
    written in a different language (English included) is translated into the book's; any other is searched as is."""
    q = query.strip()
    target = None
    if not booklang.is_english(book_language):
        lang = clear_language(q) if q else None
        if lang is None or ISO_639_3.get(lang[0]) == ("zho" if book_language == "cmn" else book_language):
            return query
        target = booklang.name(book_language)
    elif not q or foreign_language(q) is None:
        return query
    key = q if target is None else f"{book_language}\x00{q}"
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    try:
        english = await asyncio.wait_for(translate(q[:MAX_QUERY_CHARS]) if target is None
                                         else translate(q[:MAX_QUERY_CHARS], target), TRANSLATE_TIMEOUT_S)
    except Unavailable:
        return query
    except Exception as e:  # never fail the question over its translation
        logging.getLogger(__name__).warning("question translation failed: %s", type(e).__name__)
        return query
    english = " ".join(english.split()).strip("\"'“”")
    if not english:
        return query
    _cache[key] = english
    while len(_cache) > MAX_CACHE:
        _cache.popitem(last=False)
    return english
