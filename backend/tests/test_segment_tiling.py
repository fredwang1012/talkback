"""Regression: long novel uploads (Dune Messiah, Children of Dune) failed the whole segment stage with a ValueError.

ttschunks.chunk_paragraph raises when a segment's text is not found, in order, in its paragraph. Two passes built such
segments from real text: the attribution pass re-joined narration with a single space (a quote glued to an em dash,
'shrugged—"nothing', or a closing mark right after a comma, 'dark," Alia'), and validate_segments re-added a quotation
mark the model left out without the space that followed it ('" Edric asked'). The texts below are invented but have the
same shapes as the failing paragraphs.
"""
import asyncio
import logging

import pytest

from app.enrich import attribute as at
from app.enrich import pipeline
from app.enrich.segment import SegItem, validate_segments
from app.schema import Book, Character, Paragraph, Section, Segment
from app.ttschunks import chunk_paragraph, rechunk_book


def cast() -> list[Character]:
    return [Character(id="harry", name="Harry", aliases=["Harry"], first_para=0, voice="oliver", persona="p"),
            Character(id="ron", name="Ron", aliases=["Ron"], first_para=0, voice="jake", persona="p")]


def book_with(text: str, segments: list[Segment]) -> Book:
    return Book(id="t", title="T", author="a", kind="novel", license="x", characters=cast(),
                sections=[Section(id="s", title="S", start_para=0)],
                paragraphs=[Paragraph(idx=0, section_id="s", text=text, segments=segments)])


def tiles(text: str, segments: list[Segment]) -> bool:
    """The walk chunk_paragraph does: every segment found in order, and nothing but whitespace left over."""
    cursor, covered = 0, []
    for s in segments:
        t = s.text.strip()
        at_ = text.find(t, cursor)
        if at_ < 0:
            return False
        covered.append(text[cursor:at_])
        cursor = at_ + len(t)
    covered.append(text[cursor:])
    return not "".join(covered).strip()


def N(text: str, speaker: str = "narrator") -> Segment:
    return Segment(speaker=speaker, text=text)


def test_attribution_keeps_a_quote_glued_to_an_em_dash_verbatim():
    text = '"I looked everywhere," Harry said. "I asked the owls. Yet"—he shrugged —"nothing helps."'
    b = book_with(text, [N('"I looked everywhere,"', "harry"), N("Harry said."), N('"I asked the owls. Yet"'),
                         N("—he shrugged —"), N('"nothing helps."')])
    at.attribute_book(b)
    p = b.paragraphs[0]
    assert any(s.speaker == "harry" and s.text.startswith('"I asked') for s in p.segments)  # still attributed
    assert tiles(text, p.segments)
    chunk_paragraph(p, b)  # used to raise ValueError


def test_attribution_of_a_narrator_only_paragraph_with_dashes_round_a_quote():
    text = '"Hi there," said Harry. Ron nodded—"Fine, thanks"—and left.'
    b = book_with(text, [N(text)])  # the segmenter fell back to the narrator for this paragraph
    at.attribute_book(b)
    p = b.paragraphs[0]
    assert p.segments[0].speaker == "harry"
    assert tiles(text, p.segments)
    chunk_paragraph(p, b)


def test_attribution_merging_narration_keeps_a_closing_mark_with_no_space_before_it():
    text = '"We should go," Harry said. Ron frowned. It is like walking in the dark," he thought.'
    b = book_with(text, [N('"We should go,"'), N("Harry said. Ron frowned. It is like walking in the dark,"),
                         N('" he thought.')])
    at.attribute_book(b)
    p = b.paragraphs[0]
    assert p.segments[0].speaker == "harry"
    assert tiles(text, p.segments)
    chunk_paragraph(p, b)


def test_restored_quotation_mark_keeps_the_space_after_it():
    text = 'Is that where he died?" Edric asked, pointing at the map.'
    items = [SegItem(speaker="narrator", text="Is that where he died?", emotion=None),
             SegItem(speaker="harry", text="Edric asked, pointing at the map.", emotion=None)]
    segs = validate_segments(text, items, {"harry", "narrator"})
    assert segs is None or tiles(text, segs)
    if segs:
        chunk_paragraph(book_with(text, segs).paragraphs[0], book_with(text, segs))


def test_validated_segments_always_tile_or_are_rejected():
    text = '"Hush, " Ron said. "Wait."'
    for items in ([SegItem(speaker="ron", text="Hush,", emotion=None), SegItem(speaker="narrator", text="Ron said.", emotion=None),
                   SegItem(speaker="ron", text="Wait.", emotion=None)],
                  [SegItem(speaker="ron", text='"Hush, "', emotion=None), SegItem(speaker="narrator", text="Ron said.", emotion=None),
                   SegItem(speaker="ron", text="Wait.", emotion=None)]):
        segs = validate_segments(text, items, {"ron", "narrator"})
        assert segs is None or tiles(text, segs), segs


def test_rechunk_book_non_strict_reads_a_mismatched_paragraph_with_the_narrator_instead_of_raising(caplog):
    good = Paragraph(idx=1, section_id="s", text='"Yes," said Ron.', segments=[N('"Yes,"', "ron"), N("said Ron.")])
    b = book_with("Harry ran.", [N("Harry walked.")])  # a segment that is not in its paragraph
    b.paragraphs.append(good)
    with pytest.raises(ValueError):
        rechunk_book(b)  # strict by default: the load-time upgrade keeps a broken file as it is
    with caplog.at_level(logging.WARNING):
        rechunk_book(b, strict=False)
    bad = b.paragraphs[0]
    assert [(s.speaker, s.text) for s in bad.segments] == [("narrator", "Harry ran.")]
    assert [c.speaker for c in bad.tts_chunks] == ["narrator"]
    assert [c.speaker for c in good.tts_chunks] == ["ron", "narrator"]
    assert "paragraph 0" in caplog.text and "Harry walked" not in caplog.text  # never logs book text


def test_segment_stage_survives_attribution_of_dash_glued_quotes():
    text = '"Hi there," said Harry. Ron nodded—"Fine, thanks"—and left.'
    b = book_with(text, [N(text)])
    found = pipeline.attribute_speakers(b)
    pipeline.rebuild_chunks(b)
    assert found and b.paragraphs[0].tts_chunks
    assert b.paragraphs[0].segments[0].speaker == "harry"


def test_failure_log_names_the_exception_type_and_where_but_not_its_message(tmp_path, monkeypatch, caplog):
    from app.config import settings
    from app import books, ingest_upload
    monkeypatch.setattr(settings, "data_dir", tmp_path)
    book = ingest_upload.build_book("u-test-aaaaaaaa", "T", "Me", "novel", "Chapter 1\n\nHarry ran home.\n")
    book.characters = cast()
    (tmp_path / "books" / book.id).mkdir(parents=True)
    books.save_book(tmp_path / "books" / book.id / "book.json", book)

    def boom(book):
        raise ValueError("secret paragraph text")

    monkeypatch.setattr(pipeline, "attribute_speakers", boom)

    class NoLLM:
        usd, calls = 0.0, 0

    async def run():
        async def cast_ok(book, llm):
            return None
        monkeypatch.setattr(pipeline.cast_mod, "cast_novel", cast_ok)

        async def seg_ok(book, *a, **k):
            return {"skipped": 1}
        monkeypatch.setattr(pipeline.seg_mod, "segment_book", seg_ok)
        return await pipeline._run(book.id, tmp_path / "books" / book.id / "book.json", lambda **f: None, NoLLM(), None)

    with caplog.at_level(logging.WARNING):
        assert asyncio.run(run()) is False
    line = next(r.getMessage() for r in caplog.records if "failed in segment" in r.getMessage())
    assert "ValueError" in line and "test_segment_tiling.py" in line and "boom" in line
    assert "secret paragraph text" not in caplog.text
