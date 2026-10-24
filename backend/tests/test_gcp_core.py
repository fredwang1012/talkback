import json
from types import SimpleNamespace

import numpy as np
import pytest

from app.config import settings
from app.gcp import usage
from app.gcp.embed import GeminiEmbedder
from app.gcp.llm import GeminiAnswerer


@pytest.fixture(autouse=True)
def tmp_data(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    return tmp_path


def usage_lines(tmp_path):
    text = (tmp_path / "cache" / "usage.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


class FakeEmbedModels:
    def __init__(self):
        self.calls = []

    def embed_content(self, model, contents, config):
        self.calls.append((model, list(contents), config))
        return SimpleNamespace(embeddings=[SimpleNamespace(values=[0.5] * 768) for _ in contents])


def make_embedder():
    models = FakeEmbedModels()
    return GeminiEmbedder(client=SimpleNamespace(models=models)), models


def test_embed_batching_shape_dtype(tmp_data):
    emb, models = make_embedder()
    texts = [f"t{i}" for i in range(250)]
    out = emb.embed_documents(texts)
    assert [len(c[1]) for c in models.calls] == [100, 100, 50]
    assert out.shape == (250, 768) and out.dtype == np.float32
    assert all(c[2].task_type == "RETRIEVAL_DOCUMENT" and c[2].output_dimensionality == 768 for c in models.calls)
    assert sum(line["chars"] for line in usage_lines(tmp_data)) == sum(len(t) for t in texts)


def test_embed_query_task_type():
    emb, models = make_embedder()
    v = emb.embed_query("hi")
    assert v.shape == (768,) and v.dtype == np.float32
    assert models.calls[0][2].task_type == "RETRIEVAL_QUERY"


class FakeLLMModels:
    def __init__(self, chunks):
        self.chunks = chunks
        self.config = None

    async def generate_content_stream(self, model, contents, config):
        self.config = config

        async def gen():
            for c in self.chunks:
                yield c

        return gen()


def chunk(text, meta=None):
    return SimpleNamespace(text=text, usage_metadata=meta)


async def collect(chunks):
    models = FakeLLMModels(chunks)
    ans = GeminiAnswerer(client=SimpleNamespace(aio=SimpleNamespace(models=models)))
    return [s async for s in ans.stream_sentences("sys", "user")], models


async def test_sentences_across_chunks(tmp_data):
    meta = SimpleNamespace(prompt_token_count=12, candidates_token_count=7)
    out, models = await collect([chunk("Hello th"), chunk("ere. How"), chunk(" are you? Fine", meta)])
    assert out == ["Hello there.", "How are you?", "Fine"]
    assert models.config.max_output_tokens == 300
    assert models.config.thinking_config.thinking_budget == 0
    assert models.config.system_instruction == "sys"
    line = usage_lines(tmp_data)[-1]
    assert line["kind"] == "llm" and line["prompt_tokens"] == 12 and line["output_tokens"] == 7


async def test_chinese_and_no_empty():
    out, _ = await collect([chunk("你好。"), chunk("你好吗？ "), chunk(None), chunk("  ")])
    assert out == ["你好。", "你好吗？"]


async def test_no_split_inside_number():
    out, _ = await collect([chunk("It cost 3.5 dollars. Ok")])
    assert out == ["It cost 3.5 dollars.", "Ok"]


def test_usage_never_raises(monkeypatch, tmp_path):
    f = tmp_path / "file"
    f.write_text("x")
    monkeypatch.setattr(settings, "data_dir", f)  # cache dir cannot be created under a file
    usage.record("embed", "m", chars=1)
