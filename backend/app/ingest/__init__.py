"""Turn an uploaded PDF or .txt into a narrator-only book (callers use the facade app.ingest_upload).

The modules, in pipeline order:
- common: upload limits and UploadError
- pdf: pdf_to_text, the page pipeline (its docstring lists the extraction heuristics)
- lines: a page's text lines (Line) and how consecutive lines merge into paragraphs
- layout: fonts, captions, footnotes, table cells and two-column reading order
- code: program listings (CODE_MARK)
- equations: display equations and inline formulas (EQ_MARK, inline markers), visual rows
- dropcaps: drop caps, epigraphs and drop-cap chapters
- contents: tables of contents
- headings: the heading regex, headings found by font size, running headers
- figures: labelled figure regions and captioned figures cropped for the reader
- text: extract_text (.pdf or .txt)
- book: build_book, the paragraph cap and the upload's processing step
- pictures: equation and inline-formula crops made after the book is published
- store: book folders, status.json, the kept upload and duplicate detection
"""
