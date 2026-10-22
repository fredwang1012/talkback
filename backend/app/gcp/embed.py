"""Gemini embeddings on Vertex AI (gemini-embedding-001, 768 dims)."""
import time

import numpy as np
from google import genai
from google.genai import types

from ..config import settings
from . import usage

MODEL = "gemini-embedding-001"
DIMS = 768
MAX_RETRIES = 3


def _retryable(e: Exception) -> bool:
    """Rate limit (429) or server error (5xx); anything else (bad request, auth, quota exhausted) is not retried."""
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    return isinstance(code, int) and (code == 429 or code >= 500)


class GeminiEmbedder:
    def __init__(self, client=None, model: str = MODEL, sleep=time.sleep):
        self._sleep = sleep
        self.client = client or genai.Client(vertexai=True, project=settings.gcp_project, location=settings.gcp_region)
        self.model = model

    def _embed(self, texts: list[str], task_type: str) -> np.ndarray:
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = self.client.models.embed_content(
                    model=self.model,
                    contents=texts,
                    config=types.EmbedContentConfig(task_type=task_type, output_dimensionality=DIMS),
                )
                break
            except Exception as e:
                if attempt >= MAX_RETRIES or not _retryable(e):
                    raise
                self._sleep(1.0 * 2**attempt)
        usage.record("embed", self.model, chars=sum(len(t) for t in texts))
        return np.array([e.values for e in resp.embeddings], dtype=np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        return self._embed([text], "RETRIEVAL_QUERY")[0]

    def embed_documents(self, texts: list[str], batch_size: int = 100) -> np.ndarray:
        if not texts:
            return np.zeros((0, DIMS), dtype=np.float32)
        parts = [self._embed(texts[i : i + batch_size], "RETRIEVAL_DOCUMENT") for i in range(0, len(texts), batch_size)]
        return np.vstack(parts)
