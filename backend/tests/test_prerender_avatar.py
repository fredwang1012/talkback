import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "preprocess"))
import prerender_avatar as pa  # noqa: E402

from app import tts_cache  # noqa: E402
from app.config import settings  # noqa: E402
from app.schema import Book, Paragraph, Section, TtsChunk  # noqa: E402


def make_book():
    chunks = [TtsChunk(id="p0c0", speaker="holmes", voice="oliver", tts_text="Hello there.", char_start=0, char_end=12),
              TtsChunk(id="p0c1", speaker="narrator", voice="marcus", tts_text="He said.", char_start=13, char_end=21)]
    return Book(id="demo", title="T", author="A", kind="novel", license="x",
                sections=[Section(id="s1", title="I.", start_para=0)],
                paragraphs=[Paragraph(idx=0, section_id="s1", text="Hello there. He said.", tts_chunks=chunks)])


def test_plan_needs_portrait_cached_audio_and_no_existing_clip(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    book = make_book()
    assert pa.plan(book, 0, 0) == []  # nothing yet

    (tmp_path / "portraits" / "demo").mkdir(parents=True)
    (tmp_path / "portraits" / "demo" / "holmes.png").write_bytes(b"png")
    assert pa.plan(book, 0, 0) == []  # portrait but no audio

    audio = tts_cache._paths(tts_cache.cache_key("Hello there.", "oliver"))[0]
    audio.write_bytes(b"mp3")
    work = pa.plan(book, 0, 0)
    assert [w["chunk"].id for w in work] == ["p0c0"]  # narrator has no portrait: skipped
    assert pa.estimate_usd(work) > 0

    work[0]["clip"].parent.mkdir(parents=True)
    work[0]["clip"].write_bytes(b"mp4")
    assert pa.plan(book, 0, 0) == []  # already rendered
