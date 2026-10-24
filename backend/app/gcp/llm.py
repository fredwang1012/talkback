"""Gemini on Vertex AI: stream an answer and yield it sentence by sentence."""
import re
from typing import AsyncIterator, Callable

from google import genai
from google.genai import types

from ..config import settings
from . import usage

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
                               on_usage: Callable[[int, int], None] | None = None) -> AsyncIterator[str]:
        """Yield the answer sentence by sentence. `on_usage(prompt_tokens, output_tokens)` is called once at the end."""
        def config(thinking: bool):
            return types.GenerateContentConfig(
                system_instruction=system, max_output_tokens=700,
                **({"thinking_config": types.ThinkingConfig(thinking_budget=0)} if thinking else {}),
            )

        buf = ""
        meta = None
        try:
            try:
                stream = await self.client.aio.models.generate_content_stream(
                    model=self.model, contents=user, config=config(True))
            except Exception as e:
                if "Thinking budget is not supported" not in str(e):
                    raise
                stream = await self.client.aio.models.generate_content_stream(
                    model=self.model, contents=user, config=config(False))
            async for chunk in stream:
                if getattr(chunk, "usage_metadata", None) is not None:
                    meta = chunk.usage_metadata
                if chunk.text:
                    buf += chunk.text
                    sentences, buf = _split(buf)
                    for s in sentences:
                        yield s
            if buf.strip():
                yield buf.strip()
        finally:
            if meta is not None and on_usage is not None:
                on_usage(meta.prompt_token_count or 0, meta.candidates_token_count or 0)
            if meta is not None:  # also runs when the consumer abandons the stream
                usage.record(
                    "llm",
                    self.model,
                    prompt_tokens=meta.prompt_token_count or 0,
                    output_tokens=meta.candidates_token_count or 0,
                )
