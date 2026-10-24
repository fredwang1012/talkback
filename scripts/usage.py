"""Spend report over data/cache/usage.jsonl (GCP Gemini, Anthropic, Boson).

Run from the repo root: uv run --project backend python scripts/usage.py [--gcp-budget 50] [--anthropic-budget 77.33] [--boson-budget N]
All costs are estimates; check each provider's billing console.
"""
import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
USAGE_FILE = ROOT / "data" / "cache" / "usage.jsonl"

# $ per 1M tokens (input, output)
GEMINI_PRICES = {
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-flash-lite": (0.10, 0.40),
}
GEMINI_EMBED_PRICES = {"gemini-embedding-001": 0.15}  # $ per 1M input tokens
CHARS_PER_TOKEN = 4  # the embedder logs characters, so tokens are estimated
BOSON_TTS_PER_1K_CHARS = 0.015

PROVIDER_OF = {"embed": "gcp", "llm": "gcp", "anthropic": "anthropic",
               "boson_tts": "boson", "boson_realtime_session": "boson"}
TITLES = {"gcp": "GCP (Gemini)", "anthropic": "Anthropic", "boson": "Boson", "other": "Other / unrecognised"}
ORDER = ["gcp", "anthropic", "boson", "other"]
COUNT_KEYS = ("prompt_tokens", "input_tokens", "output_tokens", "chars", "sessions")


def _anthropic_prices() -> dict:
    """PRICES from preprocess/llm.py when importable (only used for lines logged without `usd`)."""
    try:
        sys.path.insert(0, str(ROOT / "preprocess"))
        import llm  # noqa: PLC0415
        return dict(llm.PRICES)
    except Exception:
        return {}


def load(path: Path = USAGE_FILE) -> tuple[list[dict], int]:
    """Return (valid rows, number of skipped lines). A missing file is an empty log."""
    rows, skipped = [], 0
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return rows, skipped
    for raw in text.splitlines():
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
            if not isinstance(row, dict) or "kind" not in row or "model" not in row:
                raise ValueError
        except ValueError:
            skipped += 1
            continue
        rows.append(row)
    return rows, skipped


def cost_of(row: dict, anthropic_prices: dict | None = None) -> float | None:
    """Estimated USD for one line, or None when the model/kind is unknown."""
    kind, model = row["kind"], row["model"]
    try:
        if kind == "llm":
            if model not in GEMINI_PRICES:
                return None
            pin, pout = GEMINI_PRICES[model]
            return (row.get("prompt_tokens", 0) * pin + row.get("output_tokens", 0) * pout) / 1e6
        if kind == "embed":
            if model not in GEMINI_EMBED_PRICES:
                return None
            return row.get("chars", 0) / CHARS_PER_TOKEN * GEMINI_EMBED_PRICES[model] / 1e6
        if kind == "anthropic":
            if "usd" in row:
                return float(row["usd"])
            prices = anthropic_prices if anthropic_prices is not None else _anthropic_prices()
            if model not in prices:
                return None
            pin, pout = prices[model]
            return (row.get("input_tokens", 0) * pin + row.get("output_tokens", 0) * pout) / 1e6
        if kind == "boson_tts":
            return row.get("chars", 0) / 1000 * BOSON_TTS_PER_1K_CHARS
        if kind == "boson_realtime_session":
            return 0.0  # pricing unknown: sessions are counted only
    except (TypeError, ValueError):
        return None
    return None


def summarize(rows: list[dict], anthropic_prices: dict | None = None) -> dict:
    """{provider: {"calls", "cost", "unknown", "models": {(kind, model): agg}, "days": {day: agg}}}"""
    out: dict = {}
    for row in rows:
        prov = PROVIDER_OF.get(row["kind"], "other")
        p = out.setdefault(prov, {"calls": 0, "cost": 0.0, "unknown": 0, "models": {}, "days": {}})
        cost = cost_of(row, anthropic_prices)
        day = str(row.get("ts", ""))[:10] or "unknown"
        p["calls"] += 1
        if cost is None:
            p["unknown"] += 1
        else:
            p["cost"] += cost
        for key, bucket in (((row["kind"], row["model"]), p["models"]), (day, p["days"])):
            a = bucket.setdefault(key, {"calls": 0, "cost": 0.0, "unknown": 0, **{k: 0 for k in COUNT_KEYS}})
            a["calls"] += 1
            if cost is None:
                a["unknown"] += 1
            else:
                a["cost"] += cost
            for k in COUNT_KEYS:
                v = row.get(k)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    a[k] += v
    return out


def _counts(a: dict) -> str:
    return " ".join(f"{k}={a[k]:,}" for k in COUNT_KEYS if a[k])


def _money(a: dict) -> str:
    if a["unknown"] == a["calls"]:
        return "unknown"
    s = f"${a['cost']:.4f}"
    return s + " (+unknown)" if a["unknown"] else s


def render(summary: dict, skipped: int, budgets: dict[str, float | None]) -> str:
    L = []
    for prov in ORDER:
        if prov not in summary:
            continue
        p = summary[prov]
        L += [f"== {TITLES[prov]} ==", f"  calls: {p['calls']}   estimated cost: {_money(p)}", "  per model:"]
        for (kind, model), a in sorted(p["models"].items()):
            L.append(f"    {kind:<24} {model:<24} calls={a['calls']:<5} {_counts(a):<46} {_money(a)}")
        L.append("  per day:")
        for day, a in sorted(p["days"].items()):
            L.append(f"    {day:<12} calls={a['calls']:<5} {_money(a)}")
        L.append("")
    L.append("== Summary ==")
    L.append(f"  {'provider':<12} {'spent (est.)':>14} {'budget':>10} {'remaining':>12}")
    for prov in ("gcp", "anthropic", "boson"):
        spent = summary.get(prov, {}).get("cost", 0.0)
        b = budgets.get(prov)
        budget = f"${b:,.2f}" if b is not None else "-"
        remaining = f"${b - spent:,.2f}" if b is not None else "-"
        L.append(f"  {TITLES[prov]:<12} {'$' + format(spent, '.4f'):>14} {budget:>10} {remaining:>12}")
    if skipped:
        L.append(f"\n  skipped {skipped} unreadable line(s)")
    L.append("\nThese are estimates (embeddings assume 4 chars/token; Boson TTS $0.015 per 1K chars; Realtime sessions are")
    L.append("counted but not priced). Check each provider's billing console for actual spend.")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--gcp-budget", type=float, default=50.0)
    ap.add_argument("--anthropic-budget", type=float, default=77.33)
    ap.add_argument("--boson-budget", type=float, default=None)
    ap.add_argument("--file", type=Path, default=USAGE_FILE)
    args = ap.parse_args(argv)
    rows, skipped = load(args.file)
    if not rows:
        print(f"No usage recorded yet in {args.file}")
    print(render(summarize(rows), skipped, {"gcp": args.gcp_budget, "anthropic": args.anthropic_budget,
                                           "boson": args.boson_budget}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
