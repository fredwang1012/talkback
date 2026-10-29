"""Compare the PDF engines of app/pdfdoc.py (pymupdf, pdfium) on real PDFs: the upload path (extract_text, then
build_book) runs under each engine and the report shows, per book, the sections (count, and whether every title
matches), paragraphs, characters, drop caps merged, figures, equations, inline formulas and code listings, the
paragraphs one engine has that the other lacks, and the text similarity per chapter (word-level; the mean and the
worst chapter). No network, no cost, nothing written into the repo.

Usage: uv run --project backend python scripts/pdf_engine_parity.py [--site DIR] [--kind novel|textbook] [--json OUT]
       PDF [PDF ...]
--site adds a directory holding pypdfium2 to sys.path (a scratch install while it is not a project dependency).
"""
import argparse
import difflib
import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

ENGINES = ("pymupdf", "pdfium")
_LIGATURES = str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl"})


def words(text: str) -> list[str]:
    from app.parsing import strip_inline
    return strip_inline(text).translate(_LIGATURES).split()


def run(engine: str, data: bytes, kind: str, work: Path) -> dict:
    """The book built under `engine`, with the figures cropped into `work`, plus the drop caps merged per page."""
    from app import ingest_upload
    from app.config import settings
    from app.pdfdoc import open_pdf
    settings.pdf_engine = engine
    figures = work / engine / "figures"
    t0 = time.perf_counter()
    raw = ingest_upload.extract_text(data, ".pdf", figures)
    book = ingest_upload.build_book("u-parity", "Parity", "", kind, raw)
    seconds = time.perf_counter() - t0
    dropcaps = 0
    with open_pdf(data) as doc:
        for page in doc:
            blocks = page.text_blocks()
            ingest_upload._merge_drop_caps(blocks)
            dropcaps += sum(1 for b in blocks if b.get("type") == 0 for ln in b["lines"] for s in ln["spans"]
                            if s.get("dropcap"))
    paras = [p for p in book.paragraphs]
    return {
        "engine": engine, "seconds": round(seconds, 1), "book": book, "raw": raw,
        "sections": [s.title for s in book.sections],
        "paragraphs": len(paras),
        "chars": sum(len(p.text) for p in paras),
        "dropcaps": dropcaps,
        "figures": sum(1 for p in paras if p.figure) if kind == "textbook" else len(list(figures.glob("*.png"))),
        "equations": sum(1 for p in paras if p.kind == "math"),
        "inline": sum(len(p.inline_math) for p in paras),
        "code": sum(1 for p in paras if p.kind == "code"),
    }


def chapter_texts(result: dict) -> list[list[str]]:
    """The words of each section's paragraphs, in section order."""
    book = result["book"]
    starts = [s.start_para for s in book.sections] + [len(book.paragraphs)]
    if not book.sections:
        return [[w for p in book.paragraphs for w in words(p.text)]]
    return [[w for p in book.paragraphs[a:b] for w in words(p.text)] for a, b in zip(starts, starts[1:])]


def compare(a: dict, b: dict) -> dict:
    """Similarity of b to a: per chapter (aligned by order when the sections match, else as one text), and the
    paragraphs of each that the other lacks (after whitespace and ligature normalisation)."""
    same_sections = a["sections"] == b["sections"]
    ta, tb = (chapter_texts(a), chapter_texts(b)) if same_sections else \
        ([[w for ch in chapter_texts(a) for w in ch]], [[w for ch in chapter_texts(b) for w in ch]])
    sims = [difflib.SequenceMatcher(None, x, y, autojunk=False).ratio() if (x or y) else 1.0 for x, y in zip(ta, tb)]
    norm = lambda p: " ".join(words(p.text))  # noqa: E731
    pa = {norm(p) for p in a["book"].paragraphs}
    pb = {norm(p) for p in b["book"].paragraphs}
    lost_ab = sorted(pa - pb, key=len, reverse=True)
    lost_ba = sorted(pb - pa, key=len, reverse=True)
    first_diff = next(((i, x, y) for i, (x, y) in enumerate(zip(a["sections"], b["sections"])) if x != y), None)
    return {"same_sections": same_sections, "first_section_diff": first_diff, "min_sim": min(sims), "mean_sim": sum(sims) / len(sims),
            "worst_chapter": sims.index(min(sims)), "lost_a_to_b": lost_ab, "lost_b_to_a": lost_ba}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdfs", nargs="+")
    ap.add_argument("--site", help="directory holding pypdfium2 (added to sys.path)")
    ap.add_argument("--kind", choices=("novel", "textbook"), help="default: textbook when the file name says so")
    ap.add_argument("--json", help="write the per-file numbers here")
    args = ap.parse_args()
    if args.site:
        sys.path.insert(0, args.site)
    from app.config import settings
    work = Path(tempfile.mkdtemp(prefix="pdf-parity-"))
    settings.data_dir = work / "data"
    report = {}
    cols = ("engine", "seconds", "paragraphs", "chars", "dropcaps", "figures", "equations", "inline", "code")
    try:
        for path in args.pdfs:
            path = Path(path)
            kind = args.kind or ("textbook" if re.search(r"textbook|retrieval|learning|\d{4}\.\d{5}", path.name, re.I) else "novel")
            data = path.read_bytes()
            print(f"\n## {path.name} ({len(data) // 1024} KB, kind={kind})")
            results = {}
            for engine in ENGINES:
                try:
                    results[engine] = run(engine, data, kind, work)
                except Exception as e:
                    print(f"{engine}: FAILED {type(e).__name__}: {e}")
            if len(results) < 2:
                continue
            a, b = results["pymupdf"], results["pdfium"]
            cmp = compare(a, b)
            print("| " + " | ".join(cols) + " | sections |")
            print("|" + "---|" * (len(cols) + 1))
            for r in (a, b):
                print("| " + " | ".join(str(r[c]) for c in cols) + f" | {len(r['sections'])} |")
            print(f"sections match: {cmp['same_sections']}" + (f" (first difference at #{cmp['first_section_diff'][0]}: "
                  f"{cmp['first_section_diff'][1]!r} vs {cmp['first_section_diff'][2]!r})" if cmp["first_section_diff"] else ""))
            if not cmp["same_sections"] and len(a["sections"]) != len(b["sections"]):
                only_a = [t for t in a["sections"] if t not in b["sections"]][:8]
                only_b = [t for t in b["sections"] if t not in a["sections"]][:8]
                print(f"  only pymupdf: {only_a}\n  only pdfium: {only_b}")
            print(f"text similarity: mean {cmp['mean_sim']:.4%}, worst chapter #{cmp['worst_chapter']} {cmp['min_sim']:.4%}")
            print(f"paragraphs only in pymupdf: {len(cmp['lost_a_to_b'])}, only in pdfium: {len(cmp['lost_b_to_a'])}")
            for label, lost in (("pymupdf", cmp["lost_a_to_b"]), ("pdfium", cmp["lost_b_to_a"])):
                for p in lost[:3]:
                    print(f"  only {label}: {p[:160]!r}")
            report[path.name] = {"kind": kind, "pymupdf": {c: a[c] for c in cols}, "pdfium": {c: b[c] for c in cols},
                                 "sections": [len(a["sections"]), len(b["sections"])], "same_sections": cmp["same_sections"],
                                 "mean_sim": cmp["mean_sim"], "min_sim": cmp["min_sim"],
                                 "lost": [len(cmp["lost_a_to_b"]), len(cmp["lost_b_to_a"])]}
    finally:
        shutil.rmtree(work, ignore_errors=True)
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
