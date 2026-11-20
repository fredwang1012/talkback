"""Equation and inline-formula pictures (#41): cropped from the PDF after the book is published, recorded in
book.json with the readings the model gives them."""
import hashlib
from pathlib import Path

from .. import books
from ..config import settings
from ..pdfdoc import open_pdf
from ..schema import Book
from ..ttschunks import BookIndex, chunk_paragraph
from .equations import _PROSE_WORD


EQ_DPI = 200
EQ_PAD = 2.0             # points around the equation's text box (fraction bars, accents, big delimiters)
EQ_MAX_PIXELS = 4_000_000
EQ_MAX_BOOK = 1500       # pictures per book


def equation_url(book_id: str, eq_id: str, png: bytes | None = None) -> str:
    """The picture's URL; `?v=` (from its bytes) makes a browser fetch a re-made crop despite its cache."""
    version = f"?v={hashlib.sha256(png).hexdigest()[:10]}" if png else ""
    return f"/api/books/{book_id}/equations/{eq_id}.png{version}"


def equation_path(book_id: str, eq_id: str) -> Path:
    return settings.data_dir / "books" / book_id / "equations" / f"{eq_id}.png"


def _book_json(book_id: str) -> Path:
    return settings.data_dir / "books" / book_id / "book.json"


def render_equations(book_id: str, data: bytes, stop=lambda: False) -> dict[str, tuple[bytes, str]]:
    """Worker thread: crop every equation of the published book from the PDF into equations/<id>.png and record the
    picture in book.json. Returns {equation id: (PNG, extracted text)} for the model. `stop()` is checked between
    pictures (a deleted book): nothing more is written, and the folder is never recreated."""
    path = _book_json(book_id)
    try:
        book = Book.model_validate_json(books.read_bytes(path))
    except (OSError, ValueError):
        return {}
    eqs = [p for p in book.paragraphs if p.kind == "math" and p.math is not None][:EQ_MAX_BOOK]
    if not eqs:
        return {}
    out_dir = path.parent / "equations"
    try:
        out_dir.mkdir(exist_ok=True)  # never parents: a deleted book's folder must not come back
    except OSError:
        return {}
    found: dict[str, tuple[bytes, str]] = {}
    spans: dict = {}
    with open_pdf(data) as doc:
        for p in eqs:
            m = p.math
            if stop():
                return {}
            png = _crop(doc, m.page, m.bbox, spans=spans)
            if png is None:
                continue
            equation_path(book_id, m.id).write_bytes(png)
            found[m.id] = (png, p.text)
    if stop():
        return {}
    if found:
        update_equations(book_id, {k: {"image": equation_url(book_id, k, png)} for k, (png, _t) in found.items()})
    return found


def _crop(doc, page_no: int, bbox: list[float], pad: tuple[float, float] = (EQ_PAD, EQ_PAD),
          spans: dict | None = None, mask_prose: bool = False) -> bytes | None:
    """PNG of the box (padded by (x, y) points) on the page, or None when it is off the page, empty or too big.

    Text of other lines is painted out: math fonts report boxes far taller than their glyphs ("≈" reaches a line
    below its baseline), so the box can overlap the next line's letters. A span belongs to another line when its
    baseline lies outside the box; prose beside an inline formula is painted out too, up to the box's edge.
    With `mask_prose`, a word whose baseline sits inside a tall glyph's box (a sum or a root reaching the line
    above) is painted out as well. `spans` caches each page's span boxes across calls. `doc` is an open
    pdfdoc.Document."""
    if not 0 <= page_no < doc.page_count:
        return None
    page = doc.page(page_no)
    x0, y0, x1, y1 = bbox
    clip = page.clip_rect((x0 - pad[0], y0 - pad[1], x1 + pad[0], y1 + pad[1]))
    if _empty(clip) or (clip[2] - clip[0]) * (clip[3] - clip[1]) * (EQ_DPI / 72) ** 2 > EQ_MAX_PIXELS:
        return None
    raster = page.render(clip, dpi=EQ_DPI, gray=True)
    if spans is None:
        spans = {}
    if page_no not in spans:
        spans[page_no] = [(tuple(s["bbox"]), s["origin"][1], s["size"], s["text"])
                          for b in page.text_blocks()
                          for ln in b.get("lines", ()) for s in ln["spans"] if s["text"].strip()]
    anchor = main = None
    if mask_prose:
        body = [(base, size) for rect, base, size, text in spans[page_no]
                if y0 <= base <= y1 and rect[2] > x0 and rect[0] < x1 and not _PROSE_WORD.search(text)]
        if body:
            anchor = max(base for base, _size in body)
            main = max(size for _base, size in body)
    for rect, base, size, text in spans[page_no]:
        if _empty(rect) or not (rect[0] < clip[2] and clip[0] < rect[2] and rect[1] < clip[3] and clip[1] < rect[3]):
            continue
        ax0, ay0 = max(rect[0], clip[0]), max(rect[1], clip[1])   # the span's box within the clip
        ax1, ay1 = min(rect[2], clip[2]), min(rect[3], clip[3])
        # a word on the line above, tucked into a tall sum or root, is not part of the formula
        tucked = (anchor is not None and _PROSE_WORD.search(text)
                  and (base < anchor - 0.7 * main or base > anchor + 0.45 * main))
        if base < y0 or base > y1 or tucked:   # another line: only where its letters are, not its font's whole box
            ay0, ay1 = max(ay0, base - 0.78 * size), min(ay1, base + 0.25 * size)
        else:   # the formula's own line: only prose beside it, outside the box, is painted out
            if rect[0] >= x1 - 0.5:
                ax0 = max(ax0, x1)
            elif rect[2] <= x0 + 0.5:
                ax1 = min(ax1, x0)
            else:
                continue
        if not _empty((ax0, ay0, ax1, ay1)):
            raster.paint_out(ax0, ay0, ax1, ay1)
    return raster.png()


def _empty(rect: tuple) -> bool:
    return rect[0] >= rect[2] or rect[1] >= rect[3]


INLINE_MAX_BOOK = 3000   # distinct inline formulas cropped per book
INLINE_PAD = (1.0, 0.5)  # points (x, y): a taller pad reaches the descenders of the line above


def render_inline(book_id: str, data: bytes, stop=lambda: False) -> tuple[dict[str, tuple[bytes, str]],
                                                                          dict[str, list[str]]]:
    """Worker thread: crop the inline formulas of the published book, like render_equations. Formulas with the same
    extracted text and size share one picture (and one reading). Returns ({first id: (PNG, extracted text)},
    {first id: every id sharing it})."""
    path = _book_json(book_id)
    try:
        book = Book.model_validate_json(books.read_bytes(path))
    except (OSError, ValueError):
        return {}, {}
    groups: dict[tuple, list] = {}
    for p in book.paragraphs:
        for m in p.inline_math:
            x0, y0, x1, y1 = m.bbox
            key = (p.text[m.start:m.end], round(x1 - x0), round(y1 - y0))
            if key in groups:
                groups[key].append(m.id)
            elif len(groups) < INLINE_MAX_BOOK:
                groups[key] = [m, m.id]
    if not groups:
        return {}, {}
    out_dir = path.parent / "equations"
    try:
        out_dir.mkdir(exist_ok=True)  # never parents: a deleted book's folder must not come back
    except OSError:
        return {}, {}
    found: dict[str, tuple[bytes, str]] = {}
    shared: dict[str, list[str]] = {}
    spans: dict = {}
    with open_pdf(data) as doc:
        for (text, _w, _h), (m, *ids) in groups.items():
            if stop():
                return {}, {}
            png = _crop(doc, m.page, m.bbox, INLINE_PAD, spans, mask_prose=True)
            if png is None:
                continue
            equation_path(book_id, m.id).write_bytes(png)
            found[m.id] = (png, text)
            shared[m.id] = ids
    if stop():
        return {}, {}
    if found:
        update_equations(book_id, {i: {"image": equation_url(book_id, k, found[k][0])}
                                   for k, ids in shared.items() for i in ids})
    return found, shared


def update_equations(book_id: str, fields: dict[str, dict]) -> int:
    """Set MathBlock / InlineMath fields ({id: {image|latex|spoken: value}}) in book.json and re-chunk those
    paragraphs. Returns how many changed; nothing is written when book.json is gone (a deleted book)."""
    path = _book_json(book_id)
    with books.write_lock(path):   # read-modify-write: shared with every other book.json writer
        try:
            book = Book.model_validate_json(books.read_bytes(path))
        except (OSError, ValueError):
            return 0
        n = 0
        index = BookIndex(book)
        for p in book.paragraphs:
            changed = 0
            new = fields.get(p.math.id) if p.math is not None else None
            if new:
                p.math = p.math.model_copy(update=new)
                changed += 1
            for i, m in enumerate(p.inline_math):
                if new := fields.get(m.id):
                    p.inline_math[i] = m.model_copy(update=new)
                    changed += 1
            if not changed:
                continue
            p.tts_chunks = chunk_paragraph(p, book, index=index)
            n += changed
        if n:
            books.save_book(path, book)
    return n
