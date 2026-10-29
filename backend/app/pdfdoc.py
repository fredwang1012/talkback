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
further away starts a new block), so the two engines group text alike on ordinary pages. It also reads like
MuPDF where pdfium's text page differs: characters in content-stream order (pdfium sorts a line's objects by x),
glyphs pdfium drops as fake bold put back when they do not overprint, advances from the font's widths, character
boxes from the ascender and descender, a space for a gap over 0.3 em in a span of its own, a superscript flag for
a glyph raised above the line's first, and a span per fill colour.
"""
from __future__ import annotations

import ctypes
import hashlib
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
_ITALIC_NAME = re.compile(r"italic|oblique|cmti|cmsl", re.I)
_MONO_NAME = re.compile(r"courier|mono|cmtt|sftt|typewriter|consol", re.I)
_SANS_NAME = re.compile(r"sans|helvetica|arial|verdana|tahoma|calibri|segoe|courier|dingbat", re.I)
# PyMuPDF's text device constants (see the module docstring)
SPACE_DIST = 0.15        # em: a smaller gap between two glyphs is nothing
SPACE_MAX_DIST = 0.8     # em: a gap up to this is a space, beyond it a new line
BASE_MAX_DIST = 0.8      # em: a glyph this close to the baseline is on the same line
PARAGRAPH_DIST = 1.5     # em: a line further below the last one than this starts a new block
MARK_MAX_DIST = 1.0      # em: an accent further off the line than this (over the next row's glyph) is lost, as in PyMuPDF
PARA_INDENT = 0.5        # points: a new line starting this far right of the line above is indented: a new block
DUP_SHIFT = 0.1          # em: a text object pdfium dropped as a copy this close to the original is fake bold
DUP_LOOKBACK = 5         # text objects before a dropped one that pdfium compares it with
OBJ_MAX_DEPTH = 4        # form XObject nesting walked for the content order of text objects
OBJ_MAX_OBJECTS = 20_000  # page objects walked per page; past it the text keeps pdfium's order
_BULLETS = set("•·‣◦▪▫■□●○◆◇*☚☛☜☝☞☟♠♡♢♣♤♥♦♧")  # a line opening with one of these (or a digit) may be a list item


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
        self._cmaps = _CmapCache()

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
        return _PdfiumPage(self._pdf[pno], pno, self._cmaps)

    def close(self) -> None:
        self._pdf.close()


class _Char:
    __slots__ = ("c", "font", "flags", "size", "x", "y", "box", "dx", "dy", "mark", "adv", "color", "synthetic", "nomap",
                 "brk")

    def __init__(self, c, font, flags, size, x, y, box, dx, dy, adv=None, color=0, synthetic=False, nomap=False):
        self.c, self.font, self.flags, self.size = c, font, flags, size
        self.x, self.y, self.box, self.dx, self.dy = x, y, box, dx, dy
        self.mark = c >= "̀" and unicodedata.category(c) == "Mn"   # a combining accent sits on the glyph before it
        self.adv = adv   # the glyph's advance width in points, when the font gives it (else the box is used)
        self.color = color             # sRGB fill colour, as PyMuPDF gives a span's
        self.synthetic = synthetic     # a space put in for a gap over 2 x SPACE_DIST: PyMuPDF gives it a span of its own
        self.nomap = nomap             # the font gives the code no Unicode: the code stands for it (a span starts)
        self.brk = False               # an image was drawn since the text before: this starts a block, as in MuPDF

    def moved(self, ddx: float, ddy: float) -> "_Char":
        x0, y0, x1, y1 = self.box
        return _Char(self.c, self.font, self.flags, self.size, self.x + ddx, self.y + ddy,
                     (x0 + ddx, y0 + ddy, x1 + ddx, y1 + ddy), self.dx, self.dy, self.adv, self.color, False, self.nomap)


class _PdfiumPage(Page):
    def __init__(self, page, pno: int, cmaps: "_CmapCache | None" = None):
        self._page = page
        self._cmaps = cmaps if cmaps is not None else _CmapCache()
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
        fonts: dict[int, tuple] = {}   # text object -> (font name, style flags, size, direction x, direction y, ...)
        faces: dict[int, tuple[str, int]] = {}   # font -> (name, style flags)
        extents: dict[int, tuple[float, float] | None] = {}   # font -> (ascender, descender) in em, as PyMuPDF uses
        name = ctypes.create_string_buffer(256)
        fd_flags = ctypes.c_int()
        ox, oy = ctypes.c_double(), ctypes.c_double()
        rect = raw.FS_RECTF()
        matrix = raw.FS_MATRIX()
        rgba = tuple(ctypes.c_uint() for _ in range(4))
        text = (ctypes.c_ushort * (n + 1))()   # every character's Unicode in one call (UTF-16 units)
        units = text[:raw.FPDFText_GetText(tp, 0, n, text) - 1] if n else []
        if len(units) != n or any(0xD800 <= u <= 0xDFFF for u in units):   # a character outside the BMP: one by one
            units = [raw.FPDFText_GetUnicode(tp, i) for i in range(n)]
        cmaps: dict[int, dict[int, int]] = {}   # font -> glyph id -> Unicode, for glyphs the PDF gives no Unicode
        encodings: dict[int, dict[int, int]] = {}   # font -> code -> Unicode from an embedded font's built-in encoding
        cx0, _cy0, _cx1, cy1 = self._crop
        upright = self._rot == 0
        o = self._pt(0.0, 0.0)
        widths: dict[tuple[int, int], float] = {}   # (font, Unicode) -> advance per point of size, 0 when unknown
        width, asc, desc = ctypes.c_float(), ctypes.c_float(), ctypes.c_float()
        out: list[_Char] = []
        owners: list[int] = []   # the text object of each character in `out`
        fkeys: list[int] = []    # and its font
        for i in range(n):
            obj = raw.FPDFText_GetTextObject(tp, i)
            u = units[i]
            if not obj or (u in (0x20, 0x0D, 0x0A, 0x09) and raw.FPDFText_IsGenerated(tp, i)):
                continue   # a space or line break pdfium made up: spaces are put back by the rules below
            key = ctypes.addressof(obj.contents)
            style = fonts.get(key)
            if style is None:   # one font, size and writing direction per text object
                pfont = raw.FPDFTextObj_GetFont(obj)
                fkey = ctypes.addressof(pfont.contents) if pfont else 0
                if fkey not in faces:   # the name and style flags are the font's: read once per font
                    n_name = raw.FPDFText_GetFontInfo(tp, i, name, 256, ctypes.byref(fd_flags))
                    face = _SUBSET_TAG.sub("", name.raw[:max(n_name - 1, 0)].decode("utf-8", "replace"))
                    angle = ctypes.c_int()
                    slanted = bool(pfont) and raw.FPDFFont_GetItalicAngle(pfont, ctypes.byref(angle)) and angle.value != 0
                    faces[fkey] = (face, _style_flags(face, fd_flags.value, slanted))
                raw.FPDFText_GetMatrix(tp, i, matrix)
                scale = math.sqrt(abs(matrix.a * matrix.d - matrix.b * matrix.c)) or 1.0
                # the writing direction, in page coordinates (y down) with the rotation applied
                dx, dy = self._pt(matrix.a, matrix.b)
                dx, dy = dx - o[0], dy - o[1]
                norm = math.hypot(dx, dy) or 1.0
                color = (rgba[0].value << 16 | rgba[1].value << 8 | rgba[2].value) \
                    if raw.FPDFText_GetFillColor(tp, i, *rgba) else 0
                style = fonts[key] = (*faces[fkey], raw.FPDFText_GetFontSize(tp, i) * scale, dx / norm, dy / norm, color,
                                      pfont, fkey, raw.FPDFText_GetFontSize(tp, i) * math.hypot(matrix.a, matrix.b))
                if fkey not in extents:
                    extents[fkey] = _font_extents(raw, pfont, asc, desc) if pfont else None
                    # the names the embedded font's own encoding gives its codes, for the codes pdfium's glyph list
                    # does not know ("prime" in TeX's CMSY): pdfium then gives the code itself, PyMuPDF the name's
                    encodings[fkey] = self._cmaps.encoding(_font_program(raw, pfont, ENCODING_FONT_MAX)) if pfont else {}
            font, flags, size, dx, dy, color, pfont, fkey, hsize = style
            nomap = bool(raw.FPDFText_HasUnicodeMapError(tp, i))
            if u in (0, 0xFFFE, 0xFFFF):   # no ToUnicode entry: a hyphen pdfium recognised, the glyph's own cmap,
                code = raw.FPDFText_GetUnicode(tp, i)   # else the code itself (as PyMuPDF does)
                if fkey not in cmaps:
                    cmaps[fkey] = self._cmaps.get(_font_program(raw, pfont)) if pfont else {}
                if raw.FPDFText_IsHyphen(tp, i) or code in cmaps[fkey]:
                    u, nomap = 0x2D if raw.FPDFText_IsHyphen(tp, i) else cmaps[fkey][code], False
                else:
                    u, nomap = code if 0 <= code < 0x110000 else 0xFFFD, True
            elif nomap and u in encodings[fkey]:
                u, nomap = encodings[fkey][u], False
            elif u == 0x02 and raw.FPDFText_IsHyphen(tp, i):
                u = 0x2D   # pdfium's mark for a hyphen ending a line
            raw.FPDFText_GetCharOrigin(tp, i, ox, oy)
            raw.FPDFText_GetLooseCharBox(tp, i, rect)
            if upright:
                x, y = ox.value - cx0, cy1 - oy.value
                box = (rect.left - cx0, cy1 - rect.top, rect.right - cx0, cy1 - rect.bottom)
            else:
                x, y = self._pt(ox.value, oy.value)
                box = self._box(rect.left, rect.bottom, rect.right, rect.top)
            # the advance from the font's widths: pdfium's loose box also covers ink past it (an italic f)
            wkey = (fkey, u)
            if wkey not in widths:
                widths[wkey] = width.value if pfont and u < 0x110000 and raw.FPDFFont_GetGlyphWidth(
                    pfont, u, 1.0, ctypes.byref(width)) and 0 < width.value < 10 else 0.0
            adv = widths[wkey] * hsize or None   # along the line: a stretched font (Tz, a scaled Tm) advances further
            out.append(_Char(chr(u), font, flags, size, x, y, box, dx, dy, adv, color, False, nomap))
            owners.append(key)
            fkeys.append(fkey)
        _quad_boxes(out, owners, [extents[f] for f in fkeys])
        # a glyph whose box lies wholly off the page is not read (PyMuPDF clips to the page)
        w, h = self.rect[2], self.rect[3]
        keep = [k for k, c in enumerate(out) if c.box[2] > 0 and c.box[0] < w and c.box[3] > 0 and c.box[1] < h]
        if len(keep) < len(out):
            out, owners = [out[k] for k in keep], [owners[k] for k in keep]
        return self._content_order(out, owners)

    def _content_order(self, chars: list[_Char], owners: list[int]) -> list[_Char]:
        """The characters in the order the content stream draws them, as PyMuPDF reads them: pdfium's text page sorts
        the text objects of a line by x (a hat drawn before the letter under it comes out after it), and leaves out
        an object that copies one of the 5 text objects before it (same codes, font and size, overlapping) as fake
        bold, though a PDF drawing each glyph as its own object has real repeats ("ff" in "effort" is two objects
        0.27 em apart). Such a copy shifted by DUP_SHIFT em or less along the line is fake bold and stays out, as
        PyMuPDF drops it; a further one gets the letters of the object it copies, moved to its own place."""
        raw = _pdfium().raw
        objects = self._text_objects(raw)
        if objects is None:
            return chars   # a page past the bound: as pdfium reads it
        texts: dict[int, list[_Char]] = {}
        for c, key in zip(chars, owners):
            texts.setdefault(key, []).append(c)
        size = ctypes.c_float()
        left, bottom, right, top = (ctypes.c_float() for _ in range(4))
        shapes: dict[int, tuple] = {}

        def shape(o, key: int) -> tuple:   # (box in the holder's space, font size, font)
            if key not in shapes:
                raw.FPDFPageObj_GetBounds(o, left, bottom, right, top)
                raw.FPDFTextObj_GetFontSize(o, size)
                font = raw.FPDFTextObj_GetFont(o)
                shapes[key] = ((left.value, bottom.value, right.value, top.value), size.value,
                               ctypes.addressof(font.contents) if font else 0)
            return shapes[key]

        o0 = self._pt(0.0, 0.0)
        out: list[_Char] = []
        placed: set[int] = set()
        brk = False
        for j, (o, key, hold, (ma, mb, mc, md), after_image) in enumerate(objects):
            brk = brk or after_image
            if key in placed:
                continue   # a form drawn twice holds the same objects: pdfium reads them once
            if key in texts:
                texts[key][0].brk = brk
                brk = False
                out.extend(texts[key])
                placed.add(key)
                continue
            (x0, y0, x1, y1), osize, ofont = shape(o, key)
            if x1 <= x0 or y1 <= y0:
                continue   # draws nothing (a space)
            seen = 0
            for po, pkey, phold, _m, _b in reversed(objects[max(0, j - 4 * DUP_LOOKBACK):j]):
                if phold != hold:
                    continue
                seen += 1
                if seen > DUP_LOOKBACK:
                    break
                if pkey not in texts:
                    continue
                (p0, q0, p1, q1), psize, pfont = shape(po, pkey)
                if psize != osize or pfont != ofont or abs((p1 - p0) - (x1 - x0)) > 0.5 or abs((q1 - q0) - (y1 - y0)) > 0.5:
                    continue
                hx, hy = x0 - p0, y0 - q0   # the shift in the holder's space, then on the page
                px, py = self._pt(ma * hx + mc * hy, mb * hx + md * hy)
                ddx, ddy = px - o0[0], py - o0[1]
                first = texts[pkey][0]
                if abs(ddx * first.dx + ddy * first.dy) <= DUP_SHIFT * (first.size or 1.0):
                    break   # drawn over itself: fake bold
                texts[key] = [c.moved(ddx, ddy) for c in texts[pkey]]
                texts[key][0].brk = brk
                brk = False
                out.extend(texts[key])
                placed.add(key)
                break
        # characters of objects the walk did not reach (past OBJ_MAX_DEPTH), where pdfium has them
        out.extend(c for c, key in zip(chars, owners) if key not in placed)
        return out

    def _text_objects(self, raw) -> list[tuple] | None:
        """(handle, key, holder key, linear matrix from the holder to page space, whether an image was drawn since
        the text object before) of the page's text objects in content order, through form XObjects OBJ_MAX_DEPTH
        deep; None when the page has more than OBJ_MAX_OBJECTS objects in all (a form drawn many times counts each
        time)."""
        out: list[tuple] = []
        seen = 0
        image = False
        m = raw.FS_MATRIX()

        def visit(form, hold: int, lin: tuple, depth: int) -> bool:
            nonlocal seen, image
            count = raw.FPDFFormObj_CountObjects(form) if form else raw.FPDFPage_CountObjects(self._page.raw)
            for i in range(max(count, 0)):
                seen += 1
                if seen > OBJ_MAX_OBJECTS:
                    return False
                o = raw.FPDFFormObj_GetObject(form, i) if form else raw.FPDFPage_GetObject(self._page.raw, i)
                if not o:
                    continue
                kind = raw.FPDFPageObj_GetType(o)
                if kind == raw.FPDF_PAGEOBJ_TEXT:
                    out.append((o, ctypes.addressof(o.contents), hold, lin, image))
                    image = False
                elif kind == raw.FPDF_PAGEOBJ_IMAGE:
                    image = True
                elif kind == raw.FPDF_PAGEOBJ_FORM and depth < OBJ_MAX_DEPTH and raw.FPDFPageObj_GetMatrix(o, m):
                    a, b, c, d = lin
                    inner = (m.a * a + m.b * c, m.a * b + m.b * d, m.c * a + m.d * c, m.c * b + m.d * d)
                    if not visit(o, ctypes.addressof(o.contents), inner, depth + 1):
                        return False
            return True

        return out if visit(None, 0, (1.0, 0.0, 0.0, 1.0), 0) else None

    def text_blocks(self) -> list[dict]:
        if self._blocks is None:
            self._blocks = _group_blocks(self._chars()) + [{"type": 1, "bbox": r} for r in self._image_rects()]
        return self._blocks

    def _bounds(self, obj, inset: float = 0.0) -> tuple[float, float, float, float]:
        """An object's box on the page, `inset` (its own units) less on every side: pdfium reports the children of
        a form XObject in the form's own space."""
        left, bottom, right, top = obj.get_bounds()
        if inset and right - left > 2 * inset and top - bottom > 2 * inset:
            left, bottom, right, top = left + inset, bottom + inset, right - inset, top - inset
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
        width = ctypes.c_float()
        rects = []
        for o in self._page.get_objects(filter=[raw.FPDF_PAGEOBJ_PATH]):
            if len(rects) >= max_paths:
                return None
            raw.FPDFPath_GetDrawMode(o, fill, stroke)
            if fill.value or stroke.value:   # a path neither filled nor stroked draws nothing
                # pdfium's box of a stroked path takes in half the line width; PyMuPDF's is the path's own
                half = width.value / 2 if stroke.value and raw.FPDFPageObj_GetStrokeWidth(o, width) else 0.0
                rects.append(self._bounds(o, half))
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


def _style_flags(font: str, fd_flags: int, slanted: bool = False) -> int:
    """PyMuPDF's span flags from a font's descriptor flags, italic angle and name, as MuPDF sets them: italic for
    a slanted font, serifed unless the name says sans (or Courier), monospaced, bold by name or ForceBold (not by
    pdfium's StemV weight guess, which calls most text bold)."""
    flags = 0
    if fd_flags & _FD_ITALIC or slanted or _ITALIC_NAME.search(font):
        flags |= 2
    if fd_flags & _FD_SERIF or not _SANS_NAME.search(font):
        flags |= 4
    if fd_flags & _FD_FIXED_PITCH or _MONO_NAME.search(font):
        flags |= 8
    if fd_flags & _FD_FORCE_BOLD or _BOLD_NAME.search(font):
        flags |= 16
    return flags


def _font_extents(raw, font, asc, desc) -> tuple[float, float] | None:
    """A font's ascender and descender (em) as PyMuPDF puts them in a character's box: the font descriptor's, scaled
    to one em together when they span less (PyMuPDF's quad correction); None when the font gives none usable."""
    if not (raw.FPDFFont_GetAscent(font, 1.0, asc) and raw.FPDFFont_GetDescent(font, 1.0, desc)):
        return None
    a, d = asc.value, desc.value
    if not (0 < a - d <= 4 and a > 0):
        return None
    return (a, d) if a - d >= 1 else (a / (a - d), d / (a - d))


def _quad_boxes(chars: list[_Char], owners: list[int], extents: list) -> None:
    """Give each character PyMuPDF's box: from its origin along the writing direction by its advance, up by the
    font's ascender and down by its descender. The letters pdfium gives a ligature (one origin, one object) share
    the ligature glyph's advance, read off pdfium's box, as does a glyph whose font gives no width. A font without
    usable extents keeps pdfium's box."""
    k = 0
    while k < len(chars):
        j = k
        first = chars[k]
        while j + 1 < len(chars) and owners[j + 1] == owners[k] and (chars[j + 1].x, chars[j + 1].y) == (first.x, first.y):
            j += 1
        for c, ext in zip(chars[k:j + 1], extents[k:j + 1]):
            x0, y0, x1, y1 = c.box
            # how far pdfium's box reaches along the writing direction: past the advance when ink overhangs it, so
            # a font width beyond it is not this glyph's (a code the font maps back from the Unicode wrongly)
            reach = max(x1 - c.x if c.dx > 0.5 else c.x - x0 if c.dx < -0.5 else y1 - c.y if c.dy > 0 else c.y - y0, 0.0)
            if j > k or not c.adv or c.nomap or c.adv > reach + 0.01:   # (a code without Unicode: no width by it)
                c.adv = reach
            if ext is None:
                continue
            up_x, up_y = c.dy * c.size, -c.dx * c.size   # one em up from the baseline (y grows downwards)
            ex, ey = c.dx * c.adv, c.dy * c.adv
            xs = (c.x + up_x * ext[0], c.x + up_x * ext[1])
            ys = (c.y + up_y * ext[0], c.y + up_y * ext[1])
            xs += (xs[0] + ex, xs[1] + ex)
            ys += (ys[0] + ey, ys[1] + ey)
            c.box = (min(xs), min(ys), max(xs), max(ys))
        k = j + 1


def _near_pen(ch: _Char, pen: tuple[float, float]) -> bool:
    """`ch` sits less than MARK_MAX_DIST em off the baseline the pen is on."""
    size = ch.size or 1.0
    delta = (ch.x - pen[0], ch.y - pen[1])
    base = (-ch.dy * delta[0] + ch.dx * delta[1]) / size
    return abs(base) < MARK_MAX_DIST


def _may_add_space(last: str) -> bool:
    """A synthetic space may follow this character (never a space; never scripts without word spaces)."""
    return last != " " and not (0x700 <= ord(last) <= 0x1FFF or ord(last) > 0x20CF)


def _font_program(raw, font, max_size: int | None = None) -> bytes:
    """The embedded font file of a pdfium font handle, or b"" (a standard font, one pdfium cannot give, or one over
    `max_size` bytes)."""
    size = ctypes.c_size_t()
    if not raw.FPDFFont_GetFontData(font, None, 0, ctypes.byref(size)) or not size.value \
            or (max_size is not None and size.value > max_size):
        return b""
    buf = (ctypes.c_ubyte * size.value)()
    return bytes(buf) if raw.FPDFFont_GetFontData(font, buf, size.value, ctypes.byref(size)) else b""


CMAP_BUDGET = 0x10000   # codes visited per font program, over every subtable: a crafted cmap costs no more
CMAP_FONTS = 256        # distinct font programs whose cmap is read per document; later ones map codes as they are


class _CmapCache:
    """The reverse cmaps of one document, per font program (pages share their fonts), at most CMAP_FONTS of them:
    so a crafted PDF cannot make the cmaps cost more than CMAP_FONTS x CMAP_BUDGET codes in all."""

    def __init__(self):
        self._by_program: dict[bytes, dict[int, int]] = {}
        self._encodings: dict[bytes, dict[int, int]] = {}

    def get(self, program: bytes) -> dict[int, int]:
        return self._read(self._by_program, program, _reverse_cmap)

    def encoding(self, program: bytes) -> dict[int, int]:
        return self._read(self._encodings, program, _builtin_encoding)

    @staticmethod
    def _read(cache: dict, program: bytes, parse) -> dict[int, int]:
        key = hashlib.sha256(program).digest()
        if key not in cache:
            if len(cache) >= CMAP_FONTS:
                return {}
            cache[key] = parse(program)
        return cache[key]


TYPE1_HEAD = 0x10000   # bytes of a Type 1 font program searched for its built-in encoding (it precedes eexec)
ENCODING_FONT_MAX = 1 << 20   # bytes: a bigger font program (a CJK font) is not read for its built-in encoding
_ENCODING_ENTRY = re.compile(rb"dup\s+(\d{1,3})\s*/([A-Za-z0-9_.]{1,64})\s+put")
CFF_STANDARD_STRINGS = 391   # string ids below this name the CFF standard strings (glyph names pdfium knows)
# TeX glyph names outside the Adobe Glyph List that PyMuPDF (MuPDF) also maps, and whose Unicode names differ
_TEX_GLYPHS = {"angbracketleft": 0x27E8, "angbracketright": 0x27E9, "bardbl": 0x2225, "negationslash": 0x0338,
               "vector": 0x20D7}


def _builtin_encoding(data: bytes) -> dict[int, int]:
    """Code -> Unicode from the built-in encoding of a Type 1 font program ("dup 48 /prime put") or a bare CFF one
    (its encoding and charset), for the glyph names in _TEX_GLYPHS, the Unicode character names ("prime") and
    uniXXXX / uXXXXX; {} for any other program."""
    if data.startswith(b"%!"):
        head = data[:TYPE1_HEAD]
        end = head.find(b"eexec")
        names = [(int(code), name.decode("ascii")) for code, name in
                 _ENCODING_ENTRY.findall(head[:end] if end >= 0 else head)]
    elif data[:1] == b"\x01":
        names = _cff_encoding(data)
    else:
        return {}
    out: dict[int, int] = {}
    for code, name in names:
        u = _unicode_of_glyph(name)
        if code < 256 and u is not None:
            out[code] = u
    return out


def _cff_index(data: bytes, pos: int) -> tuple[list[bytes], int]:
    """The entries of the CFF INDEX at `pos`, and the position after it."""
    count = struct.unpack(">H", data[pos:pos + 2])[0]
    if not count:
        return [], pos + 2
    size = data[pos + 2]
    if not 1 <= size <= 4:
        raise ValueError("bad CFF offset size")
    offs = [int.from_bytes(data[pos + 3 + k * size:pos + 3 + (k + 1) * size], "big") for k in range(count + 1)]
    base = pos + 2 + (count + 1) * size
    return [data[base + offs[k]:base + offs[k + 1]] for k in range(count)], base + offs[-1]


def _cff_dict(data: bytes) -> dict[int, list[float]]:
    """A CFF DICT: operator (12 x as 1200 + x) -> operands."""
    out, ops, k = {}, [], 0
    while k < len(data):
        b0 = data[k]
        if b0 <= 21:
            op = 1200 + data[k + 1] if b0 == 12 else b0
            k += 2 if b0 == 12 else 1
            out[op], ops = ops, []
        elif b0 == 28:
            ops.append(struct.unpack(">h", data[k + 1:k + 3])[0]); k += 3
        elif b0 == 29:
            ops.append(struct.unpack(">i", data[k + 1:k + 5])[0]); k += 5
        elif b0 == 30:   # a real: nibbles up to 0xF (the value is never needed here)
            k += 1
            while k < len(data) and (data[k] & 0x0F) != 0x0F and (data[k] >> 4) != 0x0F:
                k += 1
            ops.append(0); k += 1
        elif 32 <= b0 <= 246:
            ops.append(b0 - 139); k += 1
        elif 247 <= b0 <= 250:
            ops.append((b0 - 247) * 256 + data[k + 1] + 108); k += 2
        elif 251 <= b0 <= 254:
            ops.append(-(b0 - 251) * 256 - data[k + 1] - 108); k += 2
        else:
            k += 1
    return out


def _cff_encoding(data: bytes) -> list[tuple[int, str]]:
    """(code, glyph name) pairs of a bare CFF font's own encoding, for the glyphs named by its String INDEX (the
    custom names); [] for a CID font, a predefined encoding or charset, or a program that does not parse."""
    try:
        _names, pos = _cff_index(data, data[2])
        tops, pos = _cff_index(data, pos)
        strings, _pos = _cff_index(data, pos)
        top = _cff_dict(tops[0]) if tops else {}
        if 1230 in top or not top.get(16) or not top.get(17):   # CID-keyed, or the standard / expert encoding
            return []
        n_glyphs = struct.unpack(">H", data[int(top[17][0]):int(top[17][0]) + 2])[0]
        charset_at = int(top.get(15, [0])[0])
        if charset_at <= 2:
            return []   # a predefined charset: standard names only
        sids = [0]
        fmt, k = data[charset_at], charset_at + 1
        while len(sids) < n_glyphs:
            if fmt == 0:
                sids.append(struct.unpack(">H", data[k:k + 2])[0]); k += 2
            elif fmt in (1, 2):
                first = struct.unpack(">H", data[k:k + 2])[0]
                left = data[k + 2] if fmt == 1 else struct.unpack(">H", data[k + 2:k + 4])[0]
                k += 3 if fmt == 1 else 4
                sids.extend(range(first, first + left + 1))
            else:
                return []
        enc_at = int(top[16][0])
        fmt, k = data[enc_at], enc_at + 1
        codes: dict[int, int] = {}   # code -> glyph id
        if fmt & 0x7F == 0:
            for gid in range(1, data[k] + 1):
                codes.setdefault(data[k + gid], gid)
            k += data[k] + 1
        elif fmt & 0x7F == 1:
            gid = 1
            for r in range(data[k]):
                first, left = data[k + 1 + 2 * r], data[k + 2 + 2 * r]
                for code in range(first, min(first + left, 255) + 1):
                    codes.setdefault(code, gid)
                    gid += 1
            k += 1 + 2 * data[k]
        else:
            return []
        out = [(code, sids[gid]) for code, gid in codes.items() if gid < len(sids)]
        if fmt & 0x80:   # supplements: more codes for glyphs by name
            out += [(data[k + 1 + 3 * r], struct.unpack(">H", data[k + 2 + 3 * r:k + 4 + 3 * r])[0]) for r in range(data[k])]
        return [(code, strings[sid - CFF_STANDARD_STRINGS].decode("latin-1")) for code, sid in out
                if CFF_STANDARD_STRINGS <= sid < CFF_STANDARD_STRINGS + len(strings)]
    except (struct.error, IndexError, ValueError, KeyError):
        return []


def _unicode_of_glyph(name: str) -> int | None:
    if name in _TEX_GLYPHS:
        return _TEX_GLYPHS[name]
    m = re.fullmatch(r"uni([0-9A-F]{4})|u([0-9A-F]{4,6})", name)
    if m:
        u = int(m.group(1) or m.group(2), 16)
        return u if u < 0x110000 and not 0xD800 <= u <= 0xDFFF else None
    try:
        return ord(unicodedata.lookup(name)) if 1 < len(name) <= 64 else None
    except KeyError:
        return None


def _reverse_cmap(data: bytes) -> dict[int, int]:
    """Glyph id -> Unicode from a TrueType / OpenType font program's cmap table (formats 4 and 12), {} when the
    program has none (a bare CFF or Type 1 font). The lowest code point mapping to a glyph wins.

    The work is bounded whatever the font says: at most CMAP_BUDGET codes are visited in all (a Latin font maps a
    few hundred; a CJK font's later codes go unmapped), a subtable offset is read once, a segment running backwards
    is skipped, and a glyph id past the font's glyph count (maxp) or 65,535 is ignored."""
    out: dict[int, int] = {}
    budget = CMAP_BUDGET
    try:
        if data[:4] == b"ttcf":
            data = data[struct.unpack(">I", data[12:16])[0]:]
        tables: dict[bytes, tuple[int, int]] = {}
        for i in range(min(struct.unpack(">H", data[4:6])[0], 64)):
            tag, _check, offset, length = struct.unpack(">4sIII", data[12 + 16 * i:28 + 16 * i])
            tables.setdefault(tag, (offset, length))
        if b"cmap" not in tables:
            return {}
        n_glyphs = 0x10000
        if b"maxp" in tables:
            n_glyphs = struct.unpack(">H", data[tables[b"maxp"][0] + 4:tables[b"maxp"][0] + 6])[0] or n_glyphs
        offset, length = tables[b"cmap"]
        cmap = data[offset:offset + length]

        def put(code: int, gid: int) -> None:
            if 0 < gid < n_glyphs and (gid not in out or code < out[gid]):
                out[gid] = code

        seen: set[int] = set()
        for i in range(min(struct.unpack(">H", cmap[2:4])[0], 32)):
            platform, _encoding, sub_offset = struct.unpack(">HHI", cmap[4 + 8 * i:12 + 8 * i])
            if platform not in (0, 3) or sub_offset in seen or sub_offset >= len(cmap) or budget <= 0:
                continue
            seen.add(sub_offset)
            sub = cmap[sub_offset:]
            fmt = struct.unpack(">H", sub[:2])[0]
            if fmt == 4:
                seg2 = struct.unpack(">H", sub[6:8])[0]
                ends = struct.unpack(f">{seg2 // 2}H", sub[14:14 + seg2])
                starts = struct.unpack(f">{seg2 // 2}H", sub[16 + seg2:16 + 2 * seg2])
                deltas = struct.unpack(f">{seg2 // 2}h", sub[16 + 2 * seg2:16 + 3 * seg2])
                range_pos = 16 + 3 * seg2
                offsets = struct.unpack(f">{seg2 // 2}H", sub[range_pos:range_pos + seg2])
                for k, (start, end) in enumerate(zip(starts, ends)):
                    end = min(end, 0xFFFE)
                    if start > end:
                        continue
                    for code in range(start, min(end, start + budget - 1) + 1):
                        if offsets[k]:
                            at = range_pos + 2 * k + offsets[k] + 2 * (code - start)
                            gid = struct.unpack(">H", sub[at:at + 2])[0]
                            put(code, (gid + deltas[k]) & 0xFFFF if gid else 0)
                        else:
                            put(code, (code + deltas[k]) & 0xFFFF)
                    budget -= min(end, start + budget - 1) - start + 1
                    if budget <= 0:
                        break
            elif fmt == 12:
                for g in range(min(struct.unpack(">I", sub[12:16])[0], 20_000)):
                    start, end, gid = struct.unpack(">III", sub[16 + 12 * g:28 + 12 * g])
                    if start > end or start > 0x10FFFF:
                        continue
                    end = min(end, 0x10FFFF, start + budget - 1)
                    for code in range(start, end + 1):
                        put(code, gid + code - start)
                    budget -= end - start + 1
                    if budget <= 0:
                        break
    except (struct.error, IndexError, ValueError):
        pass
    return out


def _group_blocks(chars: list[_Char]) -> list[dict]:
    """Lines and blocks of a page from its characters (see the module docstring for the rules)."""
    blocks: list[list[list[_Char]]] = []   # block -> line -> chars
    line: list[_Char] | None = None
    ldir = (1.0, 0.0)
    pen = (0.0, 0.0)
    line_start = (0.0, 0.0)
    bullet = False
    last: _Char | None = None
    for ch in chars:
        if ch.mark and line is not None and not ch.brk:
            # a combining accent: put at the pen, as MuPDF does, so it reads before the glyph drawn after it; one
            # drawn a block away (over a glyph that starts another row) is lost, as PyMuPDF loses it
            if not _near_pen(ch, pen):
                continue
            up, down = ch.y - ch.box[1], ch.box[3] - ch.y
            ch.x, ch.y, ch.adv = pen[0], pen[1], 0.0
            ch.box = (pen[0], pen[1] - up, pen[0], pen[1] + down) if abs(ch.dy) < 1e-3 else (pen[0], pen[1]) * 2
            line.append(ch)
            last = ch   # no space follows an accent
            continue
        if line is not None and last is not None and (ch.x, ch.y) == (last.x, last.y):
            # pdfium gives a ligature as its letters at one origin, sharing the advance of the whole glyph: in place,
            # the pen past the ligature
            line.append(ch)
            last = ch
            pen = (ch.x + ch.adv * ch.dx, ch.y + ch.adv * ch.dy)
            continue
        size = ch.size or 1.0
        new_line = new_para = add_space = large = False
        if line is None or ch.brk or ch.dx * ldir[0] + ch.dy * ldir[1] < 0.999:
            new_line = new_para = True
        else:
            delta = (ch.x - pen[0], ch.y - pen[1])
            spacing = (ch.dx * delta[0] + ch.dy * delta[1]) / size
            base = (-ch.dy * delta[0] + ch.dx * delta[1]) / size   # MuPDF 1.27 (PyMuPDF 1.28): the size of this glyph
            if abs(base) < BASE_MAX_DIST:
                if SPACE_DIST <= spacing < SPACE_MAX_DIST:
                    add_space = last is not None and _may_add_space(last.c)
                    large = spacing > 2 * SPACE_DIST
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
            # as PyMuPDF puts it: at the pen, in the next glyph's font and size, reaching to that glyph
            gap = (ch.x - pen[0]) * ch.dx + (ch.y - pen[1]) * ch.dy
            up, down = ch.y - ch.box[1], ch.box[3] - ch.y
            box = (min(pen[0], ch.x), min(pen[1], ch.y) - up, max(pen[0], ch.x), max(pen[1], ch.y) + down) \
                if abs(ch.dy) < 1e-3 else (pen[0], pen[1], pen[0], pen[1])
            line.append(_Char(" ", ch.font, ch.flags, ch.size, pen[0], pen[1], box, ch.dx, ch.dy, gap, ch.color, large))
        line.append(ch)
        last = ch
        pen = (ch.x + ch.adv * ch.dx, ch.y + ch.adv * ch.dy)
    out = []
    for lines in blocks:
        dict_lines = []
        for chs in lines:
            spans = []
            # PyMuPDF's superscript flag: on a left-to-right line, a glyph raised a tenth of its size above the
            # line's first glyph
            level = chs[0].dx > 0.999999 and abs(chs[0].dy) < 1e-6
            for c in chs:
                sup = 1 if level and c.y < chs[0].y - 0.1 * c.size else 0
                key = (c.font, c.size, c.flags | sup, c.color, c.synthetic)
                # a code without Unicode starts a span (PyMuPDF flags it) that runs on past it
                if spans and spans[-1][0] == key and (not c.nomap or spans[-1][2]):
                    spans[-1][1].append(c)
                else:
                    spans.append((key, [c], c.nomap))
            dict_spans = []
            for (font, size, flags, color, _synthetic), cs, _nomap in spans:
                box = (min(c.box[0] for c in cs), min(c.box[1] for c in cs), max(c.box[2] for c in cs), max(c.box[3] for c in cs))
                first = cs[0]
                dict_spans.append({"text": "".join(c.c for c in cs), "font": font, "size": size, "flags": flags,
                                   "color": color, "bbox": box, "origin": (first.x, first.y),
                                   "ascender": (first.y - first.box[1]) / (size or 1.0),
                                   "descender": (first.y - first.box[3]) / (size or 1.0)})
            lbox = (min(s["bbox"][0] for s in dict_spans), min(s["bbox"][1] for s in dict_spans),
                    max(s["bbox"][2] for s in dict_spans), max(s["bbox"][3] for s in dict_spans))
            dict_lines.append({"spans": dict_spans, "bbox": lbox, "dir": (chs[0].dx, chs[0].dy), "wmode": 0})
        bbox = (min(ln["bbox"][0] for ln in dict_lines), min(ln["bbox"][1] for ln in dict_lines),
                max(ln["bbox"][2] for ln in dict_lines), max(ln["bbox"][3] for ln in dict_lines))
        out.append({"type": 0, "bbox": bbox, "lines": dict_lines, "number": len(out)})
    return out
