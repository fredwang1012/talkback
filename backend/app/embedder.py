"""multilingual-e5-small query/passage embeddings. Imported lazily; the backend falls back to BM25 if unavailable."""
import numpy as np

MODEL_NAME = "intfloat/multilingual-e5-small"


class E5Embedder:
    def __init__(self):
        from sentence_transformers import SentenceTransformer  # heavy import: only when actually used
        self.model = SentenceTransformer(MODEL_NAME)

    def embed_query(self, text: str) -> np.ndarray:
        return self.model.encode("query: " + text, normalize_embeddings=True)

    def embed_passages(self, texts: list[str], batch_size: int = 32) -> np.ndarray:
        return self.model.encode(["passage: " + t for t in texts], normalize_embeddings=True, batch_size=batch_size)
