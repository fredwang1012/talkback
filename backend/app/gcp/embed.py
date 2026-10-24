"""Gemini embeddings on Vertex AI (gemini-embedding-001, 768 dims)."""
import numpy as np
from google import genai
from google.genai import types

from ..config import settings
from . import usage

MODEL = "gemini-embedding-001"
DIMS = 768


class GeminiEmbedder:
    def __init__(self, client=None, model: str = MODEL):
        self.client = client or genai.Client(vertexai=True, project=settings.gcp_project, location=settings.gcp_region)
        self.model = model

    def _embed(self, texts: list[str], task_type: str) -> np.ndarray:
        resp = self.client.models.embed_content(
            model=self.model,
            contents=texts,
            config=types.EmbedContentConfig(task_type=task_type, output_dimensionality=DIMS),
        )
        usage.record("embed", self.model, chars=sum(len(t) for t in texts))
        return np.array([e.values for e in resp.embeddings], dtype=np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        return self._embed([text], "RETRIEVAL_QUERY")[0]

    def embed_documents(self, texts: list[str], batch_size: int = 100) -> np.ndarray:
        if not texts:
            return np.zeros((0, DIMS), dtype=np.float32)
        parts = [self._embed(texts[i : i + batch_size], "RETRIEVAL_DOCUMENT") for i in range(0, len(texts), batch_size)]
        return np.vstack(parts)
