"""Facade of the app.ingest package: every name callers and tests use, so the split changed no import.

The implementation lives in app/ingest (see its __init__ for the module map). Tests reach `Path` and `secrets`
through this module to patch them; the limits in app.ingest.common are tuned there, not here.
"""
import secrets  # noqa: F401
from pathlib import Path  # noqa: F401

from .ingest.book import (MAX_LEVEL, _SECTION_NUMBER, _cap_paragraphs, apply_textbook_text, build_book,  # noqa: F401
                          process_upload, publish_book, rebuild_textbook_text, section_level, split_paragraph,
                          split_spans)
from .ingest.code import (CODE_MIN_LINES, _BARE_INDEX, _CODE_NUMBERED, _CODE_SYMBOLS, _CODE_TITLE,  # noqa: F401
                          _CODE_WORDS, _code_score, _code_text, _mark_code)
from .ingest.common import (LICENSE, MAX_CHARS, MAX_PAGES, MAX_PARAGRAPH_CHARS, MAX_UPLOAD_BYTES,  # noqa: F401
                            MULTIPART_SLACK, NARRATOR_VOICE, UploadError)
from .ingest.contents import (TOC_FRACTION, TOC_MIN_LINES, _CONTENTS, _TOC_NUMBER, _TOC_TRAILING,  # noqa: F401
                              _drop_contents, _has_dot_leader, _is_toc_page, _is_toc_tail, _toc_style)
from .ingest.dropcaps import (CHAPTER_MARK, DROP_CAP, DROP_CHAPTERS, DROP_MAX_CAPS, DROP_MAX_LINES,  # noqa: F401
                              DROP_RATIO, _CHAPTER_TITLE, _DASHES, _OPENING, _PART, _keep_epigraphs, _line_key,
                              _mark_drop_chapters, _merge_drop_caps, _number_chapters)
from .ingest.equations import (DECO_BASE, EQ_INDENT, EQ_LINE_GAP, EQ_MAX_HEIGHT, EQ_MAX_PIECES, EQ_PAGE_LINES,  # noqa: F401
                               EQ_PAGE_MAX, EQ_ROW_GAP, EQ_WEAK_CHARS, INLINE_MAX_CHARS, INLINE_SHIFT, INLINE_SMALL,
                               NOT_MATH, ROW_BASE, STRONG, WEAK, _BIG_OP_FONT, _EQ_NUMBER_TAIL, _GLUE_WORD,
                               _LEAD_CATEGORIES, _MATH_FONT, _MATH_WORDS, _PROSE_WORD, _STRUCTURE_CHARS,
                               _assemble_rows, _attach_deco_rows, _by_x, _clean_piece, _deco_anchor, _equation_clip,
                               _equation_line, _inline_runs, _line_baseline, _main_line, _mark_inline, _mark_math,
                               _math_class, _needs_picture, _opens_formula, _run_bbox, _span_ink, _span_role,
                               _trail_punct_len, plain_text)
from .ingest.figures import (COLUMN_PROSE, FIGURE_DPI, FIGURE_LABELS, FIGURE_MARGIN, FIGURE_MAX_AREA,  # noqa: F401
                             FIGURE_MAX_PX, FIGURE_MIN_HEIGHT, FIGURE_PAD, MAX_CONTENT_BYTES, MAX_DRAWINGS,
                             MAX_FIGURE_RECTS, MAX_FIGURES, PROSE_EDGE, PROSE_INDENT, PROSE_WIDTH, PROSE_WORDS, THIN,
                             _FIGURE_CAPTION, _FIGURE_LABEL, _FigureRenderer, _caption_text, _captions,
                             _figure_regions, _in_figure, _overlaps, _page_figures, _take_figures, _text_column,
                             _two_column_prose, figures_dir)
from .ingest.headings import (HEADING_BIG, HEADING_BOLD, HEADING_REGEX, TOC_RUN, _BARE_LABEL, _BARE_NUMBER,  # noqa: F401
                              _CJK_HEADING, _FRONT_MATTER, _HEADING, _LABELS, _LEAD_NUM, _LEADING_DIGIT,
                              _NUMBER_WORDS, _TRAIL_NUM, _drop_running_headers, _header_core, _heading_candidate,
                              _is_formula, _is_heading, _mark_headings, _split_by_size)
from .ingest.layout import (COLUMN_GAP, SMALL_FONT, TABLE_RUN, _CAPTION, _CAPTION_START, _CELL,  # noqa: F401
                            _LEADING_SYMBOLS, _MONO_FONT, _clean_layout, _column_blocks, _font_info, _is_caption,
                            _is_mono, _reading_order, _strip_small_captions)
from .ingest.lines import (GAP_FACTOR, INDENT_PT, SHORT_FRACTION, Line, _CJK_CHAR, _CONTROL, _LIGATURES,  # noqa: F401
                           _PAGE_NUMBER, _SENTENCE_END, _clean_line_text, _join_lines, _norm_key,
                           _starts_paragraph, merge_lines)
from .ingest.pdf import pdf_to_text  # noqa: F401
from .ingest.pictures import (EQ_DPI, EQ_MAX_BOOK, EQ_MAX_PIXELS, EQ_PAD, INLINE_MAX_BOOK, INLINE_PAD,  # noqa: F401
                              _book_json, _crop, equation_path, equation_url, render_equations, render_inline,
                              update_equations)
from .ingest.store import (KIND_KEYS, _hashes_lock, _read_hashes, _write_status_file, content_hash,  # noqa: F401
                           delete_book_files, discard_book, find_upload, make_book_dir, read_status,
                           record_upload_hash, save_status, upload_path, write_status)
from .ingest.text import extract_text  # noqa: F401
from .parsing import CODE_MARK, EQ_MARK, EQ_SEP, FIGURE_MARK, HEADING_MARK  # noqa: F401
