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
    assert models.config.max_output_tokens == 700
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


def test_usage_also_emits_a_log_line_even_if_the_file_fails(monkeypatch, tmp_path, caplog):
    f = tmp_path / "file"
    f.write_text("x")
    monkeypatch.setattr(settings, "data_dir", f)
    with caplog.at_level("INFO", logger="talkback.usage"):
        usage.record("embed", "m", chars=7)
    rec = [json.loads(r.getMessage()) for r in caplog.records if r.name == "talkback.usage"]
    assert rec and rec[0]["kind"] == "embed" and rec[0]["chars"] == 7


def test_record_never_touches_the_real_usage_log(monkeypatch):
    from tests.conftest import REAL_DATA_DIR

    monkeypatch.setattr(settings, "data_dir", REAL_DATA_DIR)  # worst case: a test that forgot to redirect
    real = REAL_DATA_DIR / "cache" / "usage.jsonl"
    before = (real.stat().st_size, real.stat().st_mtime_ns) if real.exists() else None
    usage.record("boson_avatar", "m", seconds=1)
    after = (real.stat().st_size, real.stat().st_mtime_ns) if real.exists() else None
    assert after == before


def test_record_skips_file_when_usage_log_file_off(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "usage_log_file", False)
    usage.record("embed", "m", chars=1)
    assert not (tmp_path / "cache" / "usage.jsonl").exists()


def test_record_in_event_loop_appends_off_the_loop(tmp_path, monkeypatch):
    import asyncio
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    monkeypatch.setattr(settings, "usage_log_file", True)
    monkeypatch.setattr(usage, "_offload", True)

    async def go():
        usage.record("embed", "m", chars=3)
        await asyncio.sleep(0.2)
    asyncio.run(go())
    assert '"chars": 3' in (tmp_path / "cache" / "usage.jsonl").read_text(encoding="utf-8")


class _ApiError(Exception):
    def __init__(self, code):
        self.code = code


def test_embed_retries_429_and_5xx_then_succeeds():
    emb, models = make_embedder()
    real, fails = models.embed_content, [_ApiError(429), _ApiError(503)]

    def flaky(model, contents, config):
        if fails:
            raise fails.pop(0)
        return real(model, contents, config)

    models.embed_content = flaky
    sleeps = []
    emb._sleep = sleeps.append
    assert emb.embed_query("hi").shape == (768,)
    assert len(sleeps) == 2 and 0.5 <= sleeps[0] <= 1.0 and 1.0 <= sleeps[1] <= 2.0   # backoff with jitter


def test_embed_does_not_retry_client_errors_and_gives_up():
    emb, models = make_embedder()
    emb._sleep = lambda s: None
    calls = []

    def bad(code):
        def f(model, contents, config):
            calls.append(code)
            raise _ApiError(code)
        return f

    models.embed_content = bad(400)
    with pytest.raises(_ApiError):
        emb.embed_query("x")
    assert calls == [400]
    calls.clear()
    models.embed_content = bad(500)
    with pytest.raises(_ApiError):
        emb.embed_query("x")
    assert len(calls) == 4  # first try + 3 retries
