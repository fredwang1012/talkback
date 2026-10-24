"""Shared helpers for probe scripts. Run: uv run --project backend python scripts/probe_x.py"""
import datetime
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from app.boson import BosonClient, BosonError  # noqa: E402,F401
from app.config import settings  # noqa: E402,F401

CAPTURES = ROOT / "scripts" / "captures"
CAPTURES.mkdir(exist_ok=True)
_lines: list[str] = []


def note(line: str = "") -> None:
    print(line)
    _lines.append(line)


def flush_notes(title: str) -> None:
    """Append this probe's findings to docs/internal/NOTES.md."""
    with (ROOT / "docs" / "internal" / "NOTES.md").open("a", encoding="utf-8") as f:
        f.write(f"\n### Probe: {title} ({datetime.datetime.now():%Y-%m-%d %H:%M})\n")
        f.write("\n".join(f"- {l}" if l else "" for l in _lines) + "\n")


def strip_audio(evt: dict) -> dict:
    """Drop base64 audio so captures stay small."""
    evt = json.loads(json.dumps(evt))
    if isinstance(evt.get("delta"), str) and evt.get("type", "").endswith("audio.delta"):
        evt["delta"] = f"<{len(evt['delta'])} b64 chars>"
    return evt
