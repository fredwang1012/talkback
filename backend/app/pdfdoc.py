"""PDF access behind one small interface, with two engines: PyMuPDF (the default, AGPL-3.0) and pypdfium2
(PDF_ENGINE=pdfium; Apache-2.0 / BSD-3-Clause), so the ingest code never imports either library itself.

A Document has pages; a Page gives its text as PyMuPDF-style blocks / lines / spans (the structure the ingest
heuristics are written against), the rectangles of its images and vector drawings, and renders a clip to a PNG.
Coordinates are PyMuPDF's: points, origin at the page's top-left corner, y growing downwards, the page's own rotation
applied. A span is {"text", "font", "size", "flags", "bbox", "origin"} with the flags bits PyMuPDF uses
(1 superscript, 2 italic, 4 serifed, 8 monospaced, 16 bold).

The pdfium engine rebuilds lines and blocks from pdfium's characters with the rules PyMuPDF documents for its own
text device (a character continues its line when it sits within 0.8 em of the baseline; a gap of 0.15–0.8 em between
glyphs is a space, a larger one a new line; a line 0.8–1.5 em below the last one stays in the block unless it starts
right of the line above by more than half a point and that line does not open with a number or bullet; anything
further away starts a new block), so the two engines group text alike on ordinary pages.
"""
from __future__ import annotations

import ctypes
import math
import re
import struct
import unicodedata
import zlib
from pathlib import Path

from .config import settings

ENGINES = ("pymupdf", "pdfium")
_SUBSET_TAG = re.compile(r"^[A-Z]{6}\+")
# PDF font descriptor flags (ISO 32000 9.8.2)
_FD_FIXED_PITCH, _FD_SERIF, _FD_ITALIC, _FD_FORCE_BOLD = 1, 2, 1 << 6, 1 << 18
_BOLD_NAME = re.compile(r"bold|black|heavy|semibold|demibold|extrabold|ultrabold|cmbx|cmb\d|cmssbx|nimbus\w*-medi", re.I)
_ITALIC_NAME = re.compile(r"italic|oblique|cmmi|cmti|cmsl", re.I)
# PyMuPDF's text device constants (see the module docstring)
SPACE_DIST = 0.15        # em: a smaller gap between two glyphs is nothing
SPACE_MAX_DIST = 0.8     # em: a gap up to this is a space, beyond it a new line
BASE_MAX_DIST = 0.8      # em: a glyph this close to the baseline is on the same line
PARAGRAPH_DIST = 1.5     # em: a line further below the last one than this starts a new block
PARA_INDENT = 0.5        # points: a new line starting this far right of the line above is indented: a new block
_BULLETS = set("•·‣◦▪▫■□●○◆◇-–—*")  # a line opening with one of these (or a digit) may be a list item


class PdfError(Exception):
    """The bytes are not a PDF the engine can open."""


def open_pdf(data: bytes, engine: str | None = None) -> "Document":
    """Open PDF bytes with `engine` (default: settings.pdf_engine). Raises PdfError when the file cannot be read."""
    engine = engine or settings.pdf_engine
    if engine == "pdfium":
        return _PdfiumDocument(data)
    if engine == "pymupdf":
        return _MuPdfDocument(data)
    raise ValueError(f"unknown PDF engine {engine!r} (one of {', '.join(ENGINES)})")


class Document:
    """A PDF: `page_count`, `metadata` ({"title", "author"}), `toc()` ([level, title, 1-based page] rows),
    `page(i)` and iteration over the pages. Close it (or use it as a context manager) when done."""
    page_count: int

    @property
    def metadata(self) -> dict[str, str]:
        raise NotImplementedError

    def toc(self) -> list[list]:
        raise NotImplementedError

    def page(self, pno: int) -> "Page":
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def __iter__(self):
        for pno in range(self.page_count):
            yield self.page(pno)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


class Page:
    """One page: `number`, `rect` (x0, y0, x1, y1), `width`, `height`, `area`, its text, images, drawings, and
    `render(clip, ...)` to a Raster."""
    number: int
    rect: tuple[float, float, float, float]

    @property
    def width(self) -> float:
        return self.rect[2] - self.rect[0]

    @property
    def height(self) -> float:
        return self.rect[3] - self.rect[1]

    @property
    def area(self) -> float:
        return self.width * self.height

    def text_blocks(self) -> list[dict]:
        """The page's text as PyMuPDF "dict" blocks (type 0, with lines of spans) and image blocks (type 1)."""
        raise NotImplementedError

    def plain_text(self) -> str:
        """The page's text, one line per text line (a sample for a model, never parsed)."""
        raise NotImplementedError

    def clip_rect(self, box: tuple) -> tuple[float, float, float, float]:
        """`box` restricted to the page, in the engine's own precision; empty (x0 >= x1 or y0 >= y1) when off it."""
        raise NotImplementedError

    def drawing_clusters(self, max_content_bytes: int, max_paths: int) -> list[tuple] | None:
        """Rectangles around the page's vector drawings (neighbours within 3 pt joined), or None when the page is
        past a bound: its content streams above `max_content_bytes` (engines that can measure them), more than
        `max_paths` paths, or a content stream the engine cannot list."""
        raise NotImplementedError

    def render(self, clip: tuple, zoom: float | None = None, dpi: float | None = None, gray: bool = False) -> "Raster":
        """Rasterise `clip` (page points) at `zoom` times 72 dpi, or at `dpi`; grayscale with `gray`."""
        raise NotImplementedError


class Raster:
    """A rendered clip: `paint_out(x0, y0, x1, y1)` whites out a box given in page points, `png()` encodes it,
    `save(path)` writes the PNG."""

    def paint_out(self, x0: float, y0: float, x1: float, y1: float) -> None:
        raise NotImplementedError

    def png(self) -> bytes:
        raise NotImplementedError

    def save(self, path: Path) -> None:
        Path(path).write_bytes(self.png())


def cluster_rects(rects: list[tuple], page: tuple, tol: float = 3.0) -> list[tuple]:
    """Join rectangles (each within `page`) whose edges lie within `tol` points of a growing cluster, as PyMuPDF's
    cluster_drawings does: clusters are grown from the topmost-ending rect, absorbing every rect near the cluster's
    box until none is left, and a cluster thinner than `tol` in either direction (a rule) is not returned."""
    px0, py0, px1, py1 = page
    pending = []
    for r in rects:
        x0, x1 = (r[0], r[2]) if r[2] >= r[0] else (r[2], r[0])
        y0, y1 = (r[1], r[3]) if r[3] >= r[1] else (r[3], r[1])
        if x0 >= px0 and x1 <= px1 and y0 >= py0 and y1 <= py1:
            pending.append((x0, y0, x1, y1))
    pending = sorted(set(pending), key=lambda r: (r[3], r[0]))
    out = []
    while pending:
        cx0, cy0, cx1, cy1 = pending.pop(0)
        grown = True
        while grown:
            grown = False
            rest = []
            for r in pending:
                if r[2] < cx0 - tol or r[0] > cx1 + tol or r[3] < cy0 - tol or r[1] > cy1 + tol:
                    rest.append(r)
                else:
                    cx0, cy0, cx1, cy1 = min(cx0, r[0]), min(cy0, r[1]), max(cx1, r[2]), max(cy1, r[3])
                    grown = True
            pending = rest
        out.append((cx0, cy0, cx1, cy1))
    return [r for r in sorted(set(out), key=lambda r: (r[3], r[0])) if r[2] - r[0] > tol and r[3] - r[1] > tol]


def encode_png(width: int, height: int, rows: bytes, gray: bool) -> bytes:
    """A PNG of 8-bit gray or RGB pixels, `rows` being the packed scanlines (no filter bytes)."""
    stride = width * (1 if gray else 3)
    raw = b"".join(b"\x00" + rows[y * stride:(y + 1) * stride] for y in range(height))

    def chunk(tag: bytes, body: bytes) -> bytes:
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)

    head = struct.pack(">IIBBBBB", width, height, 8, 0 if gray else 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", head) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


# ---- PyMuPDF ----

class _MuPdfDocument(Document):
    def __init__(self, data: bytes):
        import pymupdf
        try:
            self._doc = pymupdf.open(stream=data, filetype="pdf")
        except Exception as e:
            raise PdfError(str(e)) from e
        self.page_count = self._doc.page_count

    @property
    def metadata(self) -> dict[str, str]:
        meta = self._doc.metadata or {}
        return {"title": meta.get("title") or "", "author": meta.get("author") or ""}

    def toc(self) -> list[list]:
        return self._doc.get_toc()

    def page(self, pno: int) -> "Page":
        return _MuPdfPage(self._doc, self._doc[pno])

    def close(self) -> None:
        self._doc.close()


class _MuPdfPage(Page):
    def __init__(self, doc, page):
        self._doc, self._page = doc, page
        self.number = page.number
        self.rect = tuple(page.rect)

    @property
    def area(self) -> float:
        return abs(self._page.rect)

    def text_blocks(self) -> list[dict]:
        return self._page.get_text("dict")["blocks"]

    def plain_text(self) -> str:
        return self._page.get_text()

    def clip_rect(self, box: tuple) -> tuple[float, float, float, float]:
        import pymupdf
        return tuple(pymupdf.Rect(box) & self._page.rect)

    def drawing_clusters(self, max_content_bytes: int, max_paths: int) -> list[tuple] | None:
        try:
            size = sum(len(self._doc.xref_stream_raw(x) or b"") for x in self._page.get_contents())
        except Exception:  # a broken content stream
            return None
        if size > max_content_bytes:
            return None
        drawings = self._page.get_drawings() if size else []
        if len(drawings) > max_paths:
            return None
        return [tuple(r) for r in self._page.cluster_drawings(drawings=drawings)] if drawings else []

    def render(self, clip: tuple, zoom: float | None = None, dpi: float | None = None, gray: bool = False) -> "Raster":
        import pymupdf
        kw = {"dpi": dpi} if dpi else {"matrix": pymupdf.Matrix(zoom, zoom)}
        if gray:
            kw["colorspace"] = pymupdf.csGRAY
        pix = self._page.get_pixmap(clip=pymupdf.Rect(clip), alpha=False, **kw)
        return _MuPdfRaster(pix, dpi / 72 if dpi else zoom)


class _MuPdfRaster(Raster):
    def __init__(self, pix, zoom: float):
        self._pix, self._zoom = pix, zoom

    def paint_out(self, x0: float, y0: float, x1: float, y1: float) -> None:
        import pymupdf
        a = pymupdf.Rect(x0, y0, x1, y1) * pymupdf.Matrix(self._zoom, self._zoom)
        self._pix.set_rect(pymupdf.IRect(a.x0, a.y0, a.x1 + 1, a.y1 + 1) & self._pix.irect, (255,) * self._pix.n)

    def png(self) -> bytes:
        return self._pix.tobytes("png")

    def save(self, path: Path) -> None:
        self._pix.save(path)


# ---- pypdfium2 ----

def _pdfium():
    import pypdfium2
    import pypdfium2.raw
    return pypdfium2


class _PdfiumDocument(Document):
    def __init__(self, data: bytes):
        pdfium = _pdfium()
        try:
            self._pdf = pdfium.PdfDocument(data)
            self.page_count = len(self._pdf)
        except Exception as e:
            raise PdfError(str(e)) from e

    @property
    def metadata(self) -> dict[str, str]:
        meta = self._pdf.get_metadata_dict()
        return {"title": meta.get("Title") or "", "author": meta.get("Author") or ""}

    def toc(self) -> list[list]:
        out = []
        for bm in self._pdf.get_toc():
            dest = bm.get_dest()
            out.append([bm.level + 1, bm.get_title(), dest.get_index() + 1 if dest is not None else -1])
        return out

    def page(self, pno: int) -> "Page":
        return _PdfiumPage(self._pdf[pno], pno)

    def close(self) -> None:
        self._pdf.close()


class _Char:
    __slots__ = ("c", "font", "flags", "size", "x", "y", "box", "dx", "dy", "mark", "gap")

    def __init__(self, c, font, flags, size, x, y, box, dx, dy, gap=False):
        self.c, self.font, self.flags, self.size = c, font, flags, size
        self.x, self.y, self.box, self.dx, self.dy = x, y, box, dx, dy
        self.mark = c >= "̀" and unicodedata.category(c) == "Mn"   # a combining accent sits on the glyph before it
        self.gap = gap   # pdfium saw a word gap before this glyph (it knows the true advance of the one before)


class _PdfiumPage(Page):
    def __init__(self, page, pno: int):
        self._page = page
        self.number = pno
        w, h = page.get_width(), page.get_height()   # with the page's rotation applied
        self.rect = (0.0, 0.0, float(w), float(h))
        self._rot = page.get_rotation()
        self._crop = page.get_cropbox()   # user space: the origin of the displayed page
        self._blocks = None

    # pdfium reports user-space coordinates (y up, the cropbox wherever it lies); the page is shown rotated
    def _pt(self, x: float, y: float) -> tuple[float, float]:
        cx0, cy0, cx1, cy1 = self._crop
        if self._rot == 90:
            return y - cy0, x - cx0
        if self._rot == 180:
            return cx1 - x, y - cy0
        if self._rot == 270:
            return cy1 - y, cx1 - x
        return x - cx0, cy1 - y

    def _box(self, left: float, bottom: float, right: float, top: float) -> tuple[float, float, float, float]:
        xs, ys = zip(self._pt(left, bottom), self._pt(right, top))
        return min(xs), min(ys), max(xs), max(ys)

    def _chars(self) -> list[_Char]:
        """The page's characters in content order, with pdfium's synthetic spaces and line breaks left out."""
        raw = _pdfium().raw
        tp = self._page.get_textpage()
        n = tp.count_chars()
        fonts: dict[int, tuple] = {}   # text object -> (font name, style flags, size, direction x, direction y)
        name = ctypes.create_string_buffer(256)
        fd_flags = ctypes.c_int()
        ox, oy = ctypes.c_double(), ctypes.c_double()
        rect = raw.FS_RECTF()
        matrix = raw.FS_MATRIX()
        text = (ctypes.c_ushort * (n + 1))()   # every character's Unicode in one call (UTF-16 units)
        units = text[:raw.FPDFText_GetText(tp, 0, n, text) - 1] if n else []
        if len(units) != n or any(0xD800 <= u <= 0xDFFF for u in units):   # a character outside the BMP: one by one
            units = [raw.FPDFText_GetUnicode(tp, i) for i in range(n)]
        cmaps: dict[int, dict[int, int]] = {}   # font -> glyph id -> Unicode, for glyphs the PDF gives no Unicode
        cx0, _cy0, _cx1, cy1 = self._crop
        upright = self._rot == 0
        o = self._pt(0.0, 0.0)
        out: list[_Char] = []
        gap = False
        for i in range(n):
            obj = raw.FPDFText_GetTextObject(tp, i)
            u = units[i]
            if not obj or (u in (0x20, 0x0D, 0x0A, 0x09) and raw.FPDFText_IsGenerated(tp, i)):
                gap = gap or u == 0x20
                continue   # a space or line break pdfium made up: spaces are put back by the rules below
            if u in (0, 0xFFFE, 0xFFFF):   # no ToUnicode entry: a hyphen pdfium recognised, the glyph's own cmap,
                code = raw.FPDFText_GetUnicode(tp, i)   # else the code itself (as PyMuPDF does)
                font = raw.FPDFTextObj_GetFont(obj)
                fkey = ctypes.addressof(font.contents) if font else 0
                if fkey not in cmaps:
                    cmaps[fkey] = _reverse_cmap(_font_program(raw, font)) if font else {}
                u = 0x2D if raw.FPDFText_IsHyphen(tp, i) else cmaps[fkey].get(code, code if 0 < code < 0x110000 else 0xFFFD)
            key = ctypes.addressof(obj.contents)
            style = fonts.get(key)
            if style is None:   # one font, size and writing direction per text object
                n_name = raw.FPDFText_GetFontInfo(tp, i, name, 256, ctypes.byref(fd_flags))
                font = _SUBSET_TAG.sub("", name.raw[:max(n_name - 1, 0)].decode("utf-8", "replace"))
                raw.FPDFText_GetMatrix(tp, i, matrix)
                scale = math.sqrt(abs(matrix.a * matrix.d - matrix.b * matrix.c)) or 1.0
                # the writing direction, in page coordinates (y down) with the rotation applied
                dx, dy = self._pt(matrix.a, matrix.b)
                dx, dy = dx - o[0], dy - o[1]
                norm = math.hypot(dx, dy) or 1.0
                style = fonts[key] = (font, _style_flags(font, fd_flags.value, raw.FPDFText_GetFontWeight(tp, i)),
                                      raw.FPDFText_GetFontSize(tp, i) * scale, dx / norm, dy / norm)
            font, flags, size, dx, dy = style
            raw.FPDFText_GetCharOrigin(tp, i, ox, oy)
            raw.FPDFText_GetLooseCharBox(tp, i, rect)
            if upright:
                x, y = ox.value - cx0, cy1 - oy.value
                box = (rect.left - cx0, cy1 - rect.top, rect.right - cx0, cy1 - rect.bottom)
            else:
                x, y = self._pt(ox.value, oy.value)
                box = self._box(rect.left, rect.bottom, rect.right, rect.top)
            out.append(_Char(chr(u), font, flags, size, x, y, box, dx, dy, gap))
            gap = False
        return out

    def text_blocks(self) -> list[dict]:
        if self._blocks is None:
            self._blocks = _group_blocks(self._chars()) + [{"type": 1, "bbox": r} for r in self._image_rects()]
        return self._blocks

    def _bounds(self, obj) -> tuple[float, float, float, float]:
        """An object's box on the page: pdfium reports the children of a form XObject in the form's own space."""
        left, bottom, right, top = obj.get_bounds()
        container = obj.container
        while container is not None:
            left, bottom, right, top = container.get_matrix().on_rect(left, bottom, right, top)
            container = container.container
        return self._box(left, bottom, right, top)

    def _image_rects(self) -> list[tuple]:
        raw = _pdfium().raw
        return [self._bounds(o) for o in self._page.get_objects(filter=[raw.FPDF_PAGEOBJ_IMAGE])]

    def plain_text(self) -> str:
        return self._page.get_textpage().get_text_range().replace("\r\n", "\n")

    def clip_rect(self, box: tuple) -> tuple[float, float, float, float]:
        x0, y0, x1, y1 = box
        return max(x0, 0.0), max(y0, 0.0), min(x1, self.rect[2]), min(y1, self.rect[3])

    def drawing_clusters(self, max_content_bytes: int, max_paths: int) -> list[tuple] | None:
        raw = _pdfium().raw
        fill, stroke = ctypes.c_int(), ctypes.c_int()
        rects = []
        for o in self._page.get_objects(filter=[raw.FPDF_PAGEOBJ_PATH]):
            if len(rects) >= max_paths:
                return None
            raw.FPDFPath_GetDrawMode(o, fill, stroke)
            if fill.value or stroke.value:   # a path neither filled nor stroked draws nothing
                rects.append(self._bounds(o))
        return cluster_rects(rects, self.rect) if rects else []

    def render(self, clip: tuple, zoom: float | None = None, dpi: float | None = None, gray: bool = False) -> "Raster":
        scale = dpi / 72 if dpi else zoom
        x0, y0, x1, y1 = clip
        w, h = self.rect[2], self.rect[3]
        bitmap = self._page.render(scale=scale, crop=(max(x0, 0.0), max(h - y1, 0.0), max(w - x1, 0.0), max(y0, 0.0)),
                                   grayscale=gray, rev_byteorder=True, may_draw_forms=False)
        return _PdfiumRaster(bitmap, scale, math.ceil(max(x0, 0.0) * scale), math.ceil(max(y0, 0.0) * scale), gray)


class _PdfiumRaster(Raster):
    def __init__(self, bitmap, scale: float, left: int, top: int, gray: bool):
        self._pixels = bitmap.to_numpy().copy()   # (rows, columns[, channels]); the bitmap may go
        bitmap.close()
        self._scale, self._left, self._top, self._gray = scale, left, top, gray

    def paint_out(self, x0: float, y0: float, x1: float, y1: float) -> None:
        h, w = self._pixels.shape[:2]
        c0, r0 = max(math.floor(x0 * self._scale) - self._left, 0), max(math.floor(y0 * self._scale) - self._top, 0)
        c1, r1 = min(math.ceil(x1 * self._scale) + 1 - self._left, w), min(math.ceil(y1 * self._scale) + 1 - self._top, h)
        if c0 < c1 and r0 < r1:
            self._pixels[r0:r1, c0:c1] = 255

    def png(self) -> bytes:
        h, w = self._pixels.shape[:2]
        return encode_png(w, h, self._pixels.tobytes(), self._gray)


def _style_flags(font: str, fd_flags: int, weight: int) -> int:
    """PyMuPDF's span flags from a font's descriptor flags, StemV weight and name."""
    flags = 0
    if fd_flags & _FD_ITALIC or _ITALIC_NAME.search(font):
        flags |= 2
    if fd_flags & _FD_SERIF:
        flags |= 4
    if fd_flags & _FD_FIXED_PITCH:
        flags |= 8
    if fd_flags & _FD_FORCE_BOLD or weight >= 600 or _BOLD_NAME.search(font):
        flags |= 16
    return flags


def _may_add_space(last: str) -> bool:
    """A synthetic space may follow this character (never a space; never scripts without word spaces)."""
    return last != " " and not (0x700 <= ord(last) <= 0x1FFF or ord(last) > 0x20CF)


def _font_program(raw, font) -> bytes:
    """The embedded font file of a pdfium font handle, or b"" (a standard font, or one pdfium cannot give)."""
    size = ctypes.c_size_t()
    if not raw.FPDFFont_GetFontData(font, None, 0, ctypes.byref(size)) or not size.value:
        return b""
    buf = (ctypes.c_ubyte * size.value)()
    return bytes(buf) if raw.FPDFFont_GetFontData(font, buf, size.value, ctypes.byref(size)) else b""


def _reverse_cmap(data: bytes) -> dict[int, int]:
    """Glyph id -> Unicode from a TrueType / OpenType font program's cmap table (formats 4 and 12), {} when the
    program has none (a bare CFF or Type 1 font). The lowest code point mapping to a glyph wins. Bounded by the
    table sizes, so a crafted font cannot make it slow."""
    try:
        if data[:4] == b"ttcf":
            data = data[struct.unpack(">I", data[12:16])[0]:]
        n_tables = struct.unpack(">H", data[4:6])[0]
        cmap = None
        for i in range(min(n_tables, 64)):
            tag, _check, offset, length = struct.unpack(">4sIII", data[12 + 16 * i:28 + 16 * i])
            if tag == b"cmap":
                cmap = data[offset:offset + length]
        if cmap is None:
            return {}
        subtables = []
        for i in range(min(struct.unpack(">H", cmap[2:4])[0], 32)):
            platform, encoding, offset = struct.unpack(">HHI", cmap[4 + 8 * i:12 + 8 * i])
            if platform in (0, 3) and offset < len(cmap):
                subtables.append(cmap[offset:])
        out: dict[int, int] = {}

        def put(code: int, gid: int) -> None:
            if gid and (gid not in out or code < out[gid]):
                out[gid] = code

        for sub in subtables:
            fmt = struct.unpack(">H", sub[:2])[0]
            if fmt == 4:
                seg2 = struct.unpack(">H", sub[6:8])[0]
                ends = struct.unpack(f">{seg2 // 2}H", sub[14:14 + seg2])
                starts = struct.unpack(f">{seg2 // 2}H", sub[16 + seg2:16 + 2 * seg2])
                deltas = struct.unpack(f">{seg2 // 2}h", sub[16 + 2 * seg2:16 + 3 * seg2])
                range_pos = 16 + 3 * seg2
                offsets = struct.unpack(f">{seg2 // 2}H", sub[range_pos:range_pos + seg2])
                for k, (start, end) in enumerate(zip(starts, ends)):
                    for code in range(start, min(end, 0xFFFE) + 1):
                        if offsets[k]:
                            at = range_pos + 2 * k + offsets[k] + 2 * (code - start)
                            gid = struct.unpack(">H", sub[at:at + 2])[0]
                            put(code, (gid + deltas[k]) & 0xFFFF if gid else 0)
                        else:
                            put(code, (code + deltas[k]) & 0xFFFF)
            elif fmt == 12:
                for g in range(min(struct.unpack(">I", sub[12:16])[0], 20_000)):
                    start, end, gid = struct.unpack(">III", sub[16 + 12 * g:28 + 12 * g])
                    for code in range(start, min(end, start + 0xFFFF) + 1):
                        put(code, gid + code - start)
        return out
    except (struct.error, IndexError, ValueError):
        return {}


def _group_blocks(chars: list[_Char]) -> list[dict]:
    """Lines and blocks of a page from its characters (see the module docstring for the rules)."""
    blocks: list[list[list[_Char]]] = []   # block -> line -> chars
    line: list[_Char] | None = None
    ldir = (1.0, 0.0)
    pen = (0.0, 0.0)
    lag_size = 0.0
    line_start = (0.0, 0.0)
    bullet = False
    last: _Char | None = None
    for ch in chars:
        if ch.mark and line is not None:
            line.append(ch)   # an accent over the glyph before it: in place, the pen stays
            continue
        size = ch.size or 1.0
        new_line = new_para = add_space = False
        if line is None or ch.dx * ldir[0] + ch.dy * ldir[1] < 0.999:
            new_line = new_para = True
        else:
            # (PyMuPDF drops a glyph drawn once more over itself, fake bold; pdfium gives a ligature as its letters
            # at one origin, which would look the same, so nothing is dropped here)
            delta = (ch.x - pen[0], ch.y - pen[1])
            spacing = (ch.dx * delta[0] + ch.dy * delta[1]) / size
            base = (-ch.dy * delta[0] + ch.dx * delta[1]) / (min(lag_size, size) or size)
            if abs(base) < BASE_MAX_DIST:
                # pdfium's loose box reaches the ink of an overhanging glyph (an f), past its advance: a gap it
                # measured as a word gap counts even when the box leaves less than SPACE_DIST of it
                if 0 < spacing < SPACE_MAX_DIST and (spacing >= SPACE_DIST or ch.gap):
                    add_space = last is not None and _may_add_space(last.c)
                elif abs(spacing) < SPACE_DIST or -SPACE_MAX_DIST < spacing < 0:
                    pass
                else:
                    new_line = True
            elif abs(base) <= PARAGRAPH_DIST:
                new_line = True
                if ch.x - line_start[0] > PARA_INDENT and not bullet:
                    new_para = True
            else:
                new_line = new_para = True
        if new_para or not blocks:
            blocks.append([])
        if new_line or line is None:
            line = []
            blocks[-1].append(line)
            ldir = (ch.dx, ch.dy)
            line_start = (ch.x, ch.y)
            bullet = ch.c.isdigit() or ch.c in _BULLETS
        elif add_space and ch.c != " " and last is not None:
            bx0, by0, bx1, by1 = last.box
            line.append(_Char(" ", last.font, last.flags, last.size, pen[0], pen[1],
                              (pen[0], by0, pen[0], by1), last.dx, last.dy))
        line.append(ch)
        last, lag_size = ch, size
        adv = (ch.box[2] - ch.box[0]) if abs(ch.dx) >= abs(ch.dy) else (ch.box[3] - ch.box[1])
        pen = (ch.x + adv * ch.dx, ch.y + adv * ch.dy)
    out = []
    for lines in blocks:
        dict_lines = []
        for chs in lines:
            spans = []
            for c in chs:
                key = (c.font, c.size, c.flags)
                if spans and spans[-1][0] == key:
                    spans[-1][1].append(c)
                else:
                    spans.append((key, [c]))
            dict_spans = []
            for (font, size, flags), cs in spans:
                box = (min(c.box[0] for c in cs), min(c.box[1] for c in cs), max(c.box[2] for c in cs), max(c.box[3] for c in cs))
                first = cs[0]
                dict_spans.append({"text": "".join(c.c for c in cs), "font": font, "size": size, "flags": flags,
                                   "bbox": box, "origin": (first.x, first.y),
                                   "ascender": (first.y - first.box[1]) / (size or 1.0),
                                   "descender": (first.y - first.box[3]) / (size or 1.0)})
            lbox = (min(s["bbox"][0] for s in dict_spans), min(s["bbox"][1] for s in dict_spans),
                    max(s["bbox"][2] for s in dict_spans), max(s["bbox"][3] for s in dict_spans))
            dict_lines.append({"spans": dict_spans, "bbox": lbox, "dir": (chs[0].dx, chs[0].dy), "wmode": 0})
        bbox = (min(ln["bbox"][0] for ln in dict_lines), min(ln["bbox"][1] for ln in dict_lines),
                max(ln["bbox"][2] for ln in dict_lines), max(ln["bbox"][3] for ln in dict_lines))
        out.append({"type": 0, "bbox": bbox, "lines": dict_lines, "number": len(out)})
    return out
