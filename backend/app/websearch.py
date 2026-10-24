"""Web search on Talk answers (Grounding with Google Search on Vertex Gemini).

Only for textbooks and for novels with spoilers on: a novel read spoiler-safe never gets the search tool, because the
web would tell the reader what happens later. Enforced here, on the server, whatever the page asks for.
Grounded answers are billed per prompt past a free tier, so they have their own persisted daily cap; past it the
answer is the normal ungrounded one."""
from .config import settings
from .daily_count import DailyCount

LOCAL_FILE = "grounded_answers.json"
PUBLIC_FILE = "public_grounded_answers.json"
MAX_SOURCES = 5
MAX_URI = 2048
MAX_TITLE = 120
MAX_SUGGESTIONS = 32_000   # Google's Search Suggestions markup (search_entry_point.rendered_content) is a few KB

local_count = DailyCount(LOCAL_FILE)


def allowed(kind: str, spoilers: bool) -> bool:
    """May this answer search the web? Textbooks always; novels only with spoilers on; never with WEB_SEARCH=false."""
    return bool(settings.web_search) and (kind == "textbook" or spoilers is True)


def take_local() -> bool:
    """Spend one grounded answer of today's local cap (False when it is used up)."""
    return local_count.take(settings.grounded_daily_cap)


def clean_sources(raw) -> list[dict]:
    """Plain {"uri", "title"} links from untrusted data: http(s) only, no duplicates, short, at most MAX_SOURCES."""
    out, seen = [], set()
    for s in raw if isinstance(raw, list) else []:
        if not isinstance(s, dict):
            continue
        uri, title = s.get("uri"), s.get("title")
        if not isinstance(uri, str) or not uri.lower().startswith(("https://", "http://")) or len(uri) > MAX_URI:
            continue
        if any(c.isspace() or ord(c) < 32 for c in uri) or uri in seen:
            continue
        seen.add(uri)
        title = " ".join(title.split())[:MAX_TITLE] if isinstance(title, str) and title.strip() else uri.split("/")[2][:MAX_TITLE]
        out.append({"uri": uri, "title": title})
        if len(out) >= MAX_SOURCES:
            break
    return out


def clean_suggestions(raw) -> str | None:
    """Google's Search Suggestions HTML, unmodified (its terms require showing it as provided), or None when it is
    missing, empty or implausibly large. The browser shows it only inside a sandboxed iframe (no scripts, no origin)."""
    if not isinstance(raw, str) or not raw.strip() or len(raw) > MAX_SUGGESTIONS:
        return None
    return raw


def suggestions_from_metadata(meta) -> str | None:
    """search_entry_point.rendered_content of a Gemini response's grounding_metadata, if any."""
    return clean_suggestions(getattr(getattr(meta, "search_entry_point", None), "rendered_content", None))


def sources_event(sources, suggestions) -> dict | None:
    """The NDJSON `sources` line for a grounded answer: its web links and Google's Search Suggestions (either may be
    missing), or None when there is neither."""
    links, html = clean_sources(sources), clean_suggestions(suggestions)
    if not links and html is None:
        return None
    return {"type": "sources", "sources": links, **({"search_suggestions": html} if html is not None else {})}


def sources_from_metadata(meta) -> list[dict]:
    """Web links (uri, title) of a Gemini response's grounding_metadata.grounding_chunks; anything odd is skipped."""
    out = []
    for ch in getattr(meta, "grounding_chunks", None) or []:
        web = getattr(ch, "web", None)
        if web is not None and getattr(web, "uri", None):
            out.append({"uri": web.uri, "title": getattr(web, "title", None) or getattr(web, "domain", None) or ""})
    return out
