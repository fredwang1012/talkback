"""Gemini on Vertex AI: stream an answer and yield it sentence by sentence."""
import logging
import re
from typing import AsyncIterator, Callable

from google import genai
from google.genai import types

from ..config import settings
from .. import websearch
from . import usage

log = logging.getLogger(__name__)

# A sentence ends at . ! ? 。 ！ ？ followed by whitespace or end of buffer.
_END = re.compile(r"[.!?。！？]+(?=\s|$)")


def _split(buf: str) -> tuple[list[str], str]:
    out, start = [], 0
    for m in _END.finditer(buf):
        s = buf[start : m.end()].strip()
        if s:
            out.append(s)
        start = m.end()
    return out, buf[start:]


class GeminiAnswerer:
    def __init__(self, client=None, model: str | None = None):
        self.client = client or genai.Client(vertexai=True, project=settings.gcp_project, location=settings.gemini_region())
        self.model = model or settings.gemini_model

    async def stream_sentences(self, system: str, user: str,
                               on_usage: Callable[[int, int], None] | None = None, *,
                               web_search: bool = False,
                               on_sources: Callable[[list[dict]], None] | None = None,
                               on_suggestions: Callable[[str], None] | None = None) -> AsyncIterator[str]:
        """Yield the answer sentence by sentence. `on_usage(prompt_tokens, output_tokens)` is called once at the end.

        `web_search` adds the Grounding with Google Search tool (callers decide when: websearch.allowed). The web links
        it used are reported once, after the last sentence, through `on_sources` (only when there are any), and
        Google's Search Suggestions markup (search_entry_point.rendered_content, unmodified) through `on_suggestions`. If the
        grounded request is refused before it starts, the answer is retried once without the tool."""
        def config(thinking: bool, grounded: bool):
            return types.GenerateContentConfig(
                system_instruction=system, max_output_tokens=700,
                **({"thinking_config": types.ThinkingConfig(thinking_budget=0)} if thinking else {}),
                **({"tools": [types.Tool(google_search=types.GoogleSearch())]} if grounded else {}),
            )

        async def start(grounded: bool):
            try:
                return await self.client.aio.models.generate_content_stream(
                    model=self.model, contents=user, config=config(True, grounded))
            except Exception as e:
                if "Thinking budget is not supported" not in str(e):
                    raise
                return await self.client.aio.models.generate_content_stream(
                    model=self.model, contents=user, config=config(False, grounded))

        buf = ""
        meta = None
        sources: list[dict] = []
        suggestions: str | None = None
        grounded = web_search
        try:
            try:
                stream = await start(grounded)
            except Exception as e:
                if not grounded:
                    raise
                log.warning("grounded answer refused (%s); answering without web search", type(e).__name__)
                grounded = False
                stream = await start(False)
            async for chunk in stream:
                if getattr(chunk, "usage_metadata", None) is not None:
                    meta = chunk.usage_metadata
                if grounded:
                    for cand in getattr(chunk, "candidates", None) or []:
                        gm = getattr(cand, "grounding_metadata", None)
                        sources += websearch.sources_from_metadata(gm)
                        suggestions = websearch.suggestions_from_metadata(gm) or suggestions
                if chunk.text:
                    buf += chunk.text
                    sentences, buf = _split(buf)
                    for s in sentences:
                        yield s
            if buf.strip():
                yield buf.strip()
            sources = websearch.clean_sources(sources)
            if sources and on_sources is not None:
                on_sources(sources)
            if suggestions and on_suggestions is not None:
                on_suggestions(suggestions)
        finally:
            if meta is not None and on_usage is not None:
                on_usage(meta.prompt_token_count or 0, meta.candidates_token_count or 0)
            if meta is not None:  # also runs when the consumer abandons the stream
                usage.record(
                    "llm",
                    self.model,
                    prompt_tokens=meta.prompt_token_count or 0,
                    output_tokens=meta.candidates_token_count or 0,
                    # billed per grounded prompt (past the free tier) only when it returned web sources
                    **({"grounded": True, "sources": len(websearch.clean_sources(sources))} if grounded else {}),
                )
