"""Gemini embeddings on Vertex AI (gemini-embedding-001, 768 dims)."""
import time

import numpy as np
from google import genai
from google.genai import types

from .. import retrying
from ..config import settings
from . import usage

MODEL = "gemini-embedding-001"
DIMS = 768
MAX_RETRIES = 3   # rate limit (429), 5xx or a network error; bad request, auth and quota exhaustion are never retried


class GeminiEmbedder:
    def __init__(self, client=None, model: str = MODEL, sleep=time.sleep):
        self._sleep = sleep
        self.client = client or genai.Client(vertexai=True, project=settings.gcp_project, location=settings.gcp_region)
        self.model = model

    def _embed(self, texts: list[str], task_type: str) -> np.ndarray:
        def once():
            return self.client.models.embed_content(
                model=self.model,
                contents=texts,
                config=types.EmbedContentConfig(task_type=task_type, output_dimensionality=DIMS),
            )
        resp = retrying.retry_sync(once, what="embedding", attempts=MAX_RETRIES + 1, base=1.0, sleep=self._sleep)
        usage.record("embed", self.model, chars=sum(len(t) for t in texts))
        return np.array([e.values for e in resp.embeddings], dtype=np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        return self._embed([text], "RETRIEVAL_QUERY")[0]

    def embed_documents(self, texts: list[str], batch_size: int = 100) -> np.ndarray:
        if not texts:
            return np.zeros((0, DIMS), dtype=np.float32)
        parts = [self._embed(texts[i : i + batch_size], "RETRIEVAL_DOCUMENT") for i in range(0, len(texts), batch_size)]
        return np.vstack(parts)
