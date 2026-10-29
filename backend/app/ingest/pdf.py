"""Turn an uploaded PDF or .txt into a narrator-only book: extract text, parse, add TTS chunks, write retrieval chunks.

PDF extraction heuristics (deliberately simple), working on text lines with their bounding boxes:
- the first and last block of each page are dropped when the same text (digits ignored) repeats on many pages
  (running headers and footers), or when they are a bare page number;
- consecutive lines (across blocks and pages) are merged into one paragraph, unless the previous line ends a
  sentence AND there is a paragraph signal: the next line is indented, there is a vertical gap larger than normal
  line spacing, or the previous line is clearly shorter than the page's text width (short last line);
- a block that is a single short heading line ("Chapter 3", "12 The Wedding", "CHAPTER ONE") stands on its own;
- a line ending in a hyphen right after a letter and followed by a lowercase letter is de-hyphenated ("exam-" + "ple");
- a captioned figure ("Figure 2.10 ...") and everything between it and the nearest prose is cropped to a PNG and
  becomes a figure paragraph (its caption), never body text (#40, see _take_figures).
Limits: pages, extracted characters and paragraph length are capped (see constants).
"""
from collections import Counter
from pathlib import Path

from ..parsing import EQ_MARK, HEADING_MARK
from . import common
from .code import _mark_code
from .common import UploadError
from .contents import _CONTENTS, _drop_contents, _is_toc_page, _is_toc_tail, _toc_style
from .dropcaps import _keep_epigraphs, _mark_drop_chapters, _merge_drop_caps, _number_chapters
from .equations import NOT_MATH, _assemble_rows, _attach_deco_rows, _by_x, _mark_inline, _mark_math, _math_class
from .figures import _FigureRenderer, _page_figures, _take_figures, _text_column
from .headings import HEADING_BIG, _BARE_NUMBER, _drop_running_headers, _mark_headings
from .layout import _clean_layout, _font_info, _is_mono, _reading_order
from .lines import _CONTROL, _PAGE_NUMBER, Line, _clean_line_text, _norm_key, merge_lines


def pdf_to_text(data: bytes, figures_out: Path | None = None) -> str:
    """Extract paragraph-separated text from PDF bytes. Raises UploadError if the PDF is unreadable or has no text.

    With `figures_out`, captioned figures are cropped into PNGs there and become FIGURE_MARK paragraphs (#40);
    without it their text is still left out of the body text."""
    import pymupdf
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as e:
        raise UploadError("The PDF could not be read.") from e
    with doc:
        if doc.page_count > common.MAX_PAGES:
            raise UploadError(f"The PDF has too many pages ({common.MAX_PAGES} maximum).")
        pages: list[list[list[tuple]]] = []  # page -> blocks -> lines (text, x0, y0, x1, y1)
        widths: list[float] = []
        total = 0
        sizes: Counter = Counter()  # font size -> characters, to find the body size
        figures: list[list[tuple]] = []  # page -> labelled figure regions (see _figure_regions)
        page_rects: list[list[tuple]] = []  # page -> images (rules too) and labelled figure regions: figure parts
        page_boxes: list[tuple] = []
        inline: dict[tuple, str] = {}  # (page, x0, y0, text) of a prose line -> its text with inline formulas marked
        drops: set[tuple] = set()      # (page, x0, y0, text) of each line that opens with a drop cap
        for page in doc:
            blocks = []
            info = []  # per line: (math class, glyphs lost), see _mark_math
            rects = []  # images and drawing clusters: figure candidates
            raw_blocks = page.get_text("dict")["blocks"]
            grouped: list[list] = []
            for b in raw_blocks:
                if b.get("type") == 1:
                    rects.append(tuple(b["bbox"]))
            _merge_drop_caps(raw_blocks)
            for b in raw_blocks:
                if b.get("type") != 0:
                    continue
                grouped.append([[spans, base] for spans, base in _assemble_rows(b["lines"])])
            _attach_deco_rows(grouped)
            for rows in grouped:
                lines, line_info = [], []
                for item in rows:
                    if item is None:
                        continue
                    spans, base = item
                    spans = _by_x(spans)
                    raw_text = "".join(s["text"] for s in spans)
                    t = _clean_line_text(raw_text)
                    if not t:
                        continue
                    x0 = min(s["bbox"][0] for s in spans)
                    y0 = min(s["bbox"][1] for s in spans)
                    x1 = max(s["bbox"][2] for s in spans)
                    y1 = max(s["bbox"][3] for s in spans)
                    size, bold = _font_info(spans)
                    sizes[size] += len(t)
                    lines.append((t, x0, y0, x1, y1, size, bold, _is_mono(spans)))
                    cls = _math_class(spans)
                    line_info.append((cls, bool(_CONTROL.search(raw_text)), base))
                    marked = _mark_inline(spans, page.number) if cls == NOT_MATH else None
                    if marked:
                        inline[(page.number, x0, y0, t)] = marked
                    if any(s.get("dropcap") for s in spans):
                        drops.add((page.number, x0, y0, t))
                    total += len(t) + 1
                    if total > common.MAX_CHARS:  # checked per line so extraction stops as soon as the cap is crossed
                        raise UploadError("The document has too much text (5 million characters maximum).")
                if lines:
                    blocks.append(lines)
                    info.append(line_info)
            blocks = _mark_math(blocks, info, page.number)
            pages.append(blocks)
            widths.append(page.rect.width)
            figures.append(_page_figures(doc, page, blocks, rects))
            page_rects.append(rects + figures[-1])
            page_boxes.append(tuple(page.rect))

    def key(block):
        return _norm_key(" ".join(ln[0] for ln in block))

    edge = Counter()
    for blocks in pages:
        for k in ({key(blocks[0]), key(blocks[-1])} if blocks else ()):
            edge[k] += 1
    repeated = {k for k, n in edge.items() if n >= 3 and n >= 0.2 * len(pages)}

    def is_noise(block) -> bool:
        text = " ".join(ln[0] for ln in block)
        if text.startswith(EQ_MARK):
            return False
        if _BARE_NUMBER.match(text) and body and max(ln[5] for ln in block) >= body * HEADING_BIG:
            return False  # a big lone number is a chapter number, not a page number
        return key(block) in repeated or bool(_PAGE_NUMBER.match(text))

    body = sizes.most_common(1)[0][0] if sizes else 0.0

    def strip_noise(blocks):
        while blocks and is_noise(blocks[0]):
            blocks = blocks[1:]
        while blocks and is_noise(blocks[-1]):
            blocks = blocks[:-1]
        return blocks

    pages = [strip_noise(blocks) for blocks in pages]
    pages = _drop_running_headers(pages, body)
    all_lines: list[Line] = []
    column = _text_column(pages, body)
    renderer = _FigureRenderer(data, figures_out)
    try:
        laid_out = []
        toc = None  # the entry style of the contents page just before, see _toc_style
        epigraph: set = set()
        for pno, blocks in enumerate(pages):
            # figures first: a table of bare numbers would otherwise make its whole page look like a contents page
            blocks = _take_figures(blocks, page_rects[pno], page_boxes[pno], body, column, renderer.page(pno))
            if _is_toc_page(blocks):
                toc = _toc_style(blocks)
                continue
            if toc is not None and _is_toc_tail(blocks, toc):
                continue
            toc = _toc_style(blocks) if any(len(b) == 1 and _CONTENTS.match(b[0][0]) for b in blocks) else None
            blocks, epi = _keep_epigraphs(_reading_order(blocks, widths[pno]), pno, drops, body)
            epigraph |= epi
            laid_out.append((pno, _mark_code(_drop_contents(_clean_layout(blocks, body)))))
    finally:
        renderer.close()
    for pno, blocks in laid_out:
        flat = [ln for blk in blocks for ln in blk]
        if not flat:
            continue
        x0s = sorted(ln[1] for ln in flat)
        x1s = sorted(ln[3] for ln in flat)
        left, right = x0s[len(x0s) // 5], x1s[len(x1s) * 9 // 10]
        for blk in _mark_drop_chapters(_mark_headings(blocks, body, figures[pno]), pno, drops, epigraph):
            alone = len(blk) == 1 and blk[0][7]
            for t, x0, y0, x1, y1, _size, _bold, _h in blk:
                all_lines.append(Line(t, x0, x1, y0, y1, left, right, pno, alone, inline.get((pno, x0, y0, t)),
                                      (pno, x0, y0, t) in drops))

    paras = _number_chapters(merge_lines(all_lines))
    paras = [p for i, p in enumerate(paras) if i == 0 or p != paras[i - 1] or not p.startswith(HEADING_MARK)]
    text = "\n\n".join(paras)
    if not text.strip():
        raise UploadError("No extractable text was found. Scanned (image-only) PDFs are not supported.")
    return text
