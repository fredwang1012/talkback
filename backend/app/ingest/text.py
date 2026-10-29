"""extract_text: the text of an uploaded .pdf or .txt."""
from pathlib import Path

from . import common
from .common import UploadError
from .pdf import pdf_to_text


def extract_text(data: bytes, ext: str, figures_out: Path | None = None) -> str:
    if ext == ".pdf":
        return pdf_to_text(data, figures_out)
    text = data.decode("utf-8-sig", errors="replace")
    if len(text) > common.MAX_CHARS:
        raise UploadError("The document has too much text (5 million characters maximum).")
    if not text.strip():
        raise UploadError("The text file is empty.")
    return text
