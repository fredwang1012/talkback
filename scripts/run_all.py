"""run_all: run every key-dependent step in the right order, once the keys are in talkback/.env.

Usage (from talkback/):
  python scripts/run_all.py --plan                 # show what would run and what is blocked, run nothing
  python scripts/run_all.py                        # Boson probes and the free stages; Anthropic stages are skipped
  python scripts/run_all.py --anthropic            # also summaries, cast, segment, spoiler eval
  python scripts/run_all.py --anthropic --yes      # no pauses, and accept spend estimates over $5

Stops at the first failing stage. Stages whose key is missing are reported as blocked, never run.
Anthropic stages need --anthropic on purpose: they spend credit, so they never run by accident.
After `cast` it pauses so you can review data/books/hp*/book.json (unless --yes).
Read-mode audio is synthesised on demand and cached; there is no prerender stage (the series is ~6 M characters).
Standard library only, so it runs under any Python 3.
"""
import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PDF = "data/uploads/harry-potter-collection.pdf"
SERIES = "harry-potter"
BOOKS = ["hp1-sorcerers-stone", "hp2-chamber-of-secrets", "hp3-prisoner-of-azkaban", "hp4-goblet-of-fire",
         "hp5-order-of-the-phoenix", "hp6-half-blood-prince", "hp7-deathly-hallows"]
EVAL_BOOK = "hp3-prisoner-of-azkaban"  # mid-series: earlier books are fair game, this one is spoiler-gated


@dataclass
class Stage:
    name: str
    needs: str | None  # "boson", "anthropic" or None
    cmd: list[str]  # run with `uv run --project backend python ...`
    why: str
    pause_after: str | None = None


def read_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def each(flag: str) -> list[str]:
    return [x for b in BOOKS for x in (flag, b)]


def build_plan(has_pdf: bool) -> list[Stage]:
    py = lambda *a: list(a)  # noqa: E731
    stages = [
        Stage("probe-secret", "boson", py("scripts/probe_secret.py"), "M0: ephemeral key prefix and expiry"),
        Stage("probe-tts", "boson", py("scripts/probe_tts.py"), "M0: timestamps, tags, voices"),
        Stage("probe-voice", "boson", py("scripts/probe_voice.py"), "M0: voice clone response shape"),
        Stage("probe-realtime", "boson", py("scripts/probe_realtime.py"), "M0: manual turns, instructions, voice change, latency"),
    ]
    if has_pdf:
        stages.append(Stage("ingest", None, py("preprocess/ingest_series.py", "--pdf", PDF, "--skip-existing"),
                            "split the collection PDF into 7 books (existing books are kept)"))
    stages += [
        Stage("summaries", "anthropic", py("preprocess/summaries.py", *each("--book")), "incremental story-so-far notes, Sonnet"),
        Stage("cast", "anthropic", py("preprocess/cast.py", "--series", SERIES), "one Sonnet call casts the whole series",
              pause_after="Review the cast in data/books/hp*/book.json (voices, personas, first_para), then continue."),
        Stage("segment", "anthropic", py("preprocess/segment.py", *each("--book")), "Haiku splits dialogue by speaker"),
        Stage("chunk", None, py("preprocess/chunk_tts.py", *each("--book")), "TTS chunks from the segments"),
        Stage("index-vertex", None, py("scripts/index_vertex.py", *each("--book")), "embed + upsert into Vector Search (GCP)"),
        Stage("eval-spoilers", "anthropic", py("preprocess/eval_spoilers.py", "--book", EVAL_BOOK, "--notes"), "M5: 0 leaks target"),
    ]
    return stages


def key_state(env: dict[str, str], allow_anthropic: bool) -> dict[str, bool]:
    return {"boson": bool(env.get("BOSON_API_KEY")), "anthropic": bool(env.get("ANTHROPIC_API_KEY")) and allow_anthropic}


def blocked_reason(stage: Stage, keys: dict[str, bool], env: dict[str, str]) -> str | None:
    if stage.needs and not keys[stage.needs]:
        if stage.needs == "anthropic" and env.get("ANTHROPIC_API_KEY"):
            return "Anthropic stages need --anthropic"
        return f"needs {stage.needs.upper()}_API_KEY in .env"
    return None


def uv_path() -> str:
    found = shutil.which("uv")
    if found:
        return found
    for cand in Path(os.environ.get("APPDATA", "")).glob("Python/Python3*/Scripts/uv*"):
        return str(cand)
    sys.exit("uv not found. Install it (pip install uv) and retry.")


def command_for(stage: Stage, assume_yes: bool) -> list[str]:
    cmd = [uv_path(), "run", "--project", "backend", "python", *stage.cmd]
    spends = stage.needs in ("anthropic", "boson") and stage.name.startswith(("segment", "summaries", "eval", "cast"))
    if assume_yes and spends and stage.name != "cast":
        cmd.append("--yes")
    return cmd


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true", help="print the plan and exit")
    ap.add_argument("--anthropic", action="store_true", help="allow stages that spend Anthropic credit")
    ap.add_argument("--yes", action="store_true", help="no pauses; accept estimates over $5")
    ap.add_argument("--only", nargs="*", help="run only these stage names")
    ap.add_argument("--skip", nargs="*", default=[], help="skip these stage names")
    args = ap.parse_args()

    env = read_env(ROOT / ".env")
    keys = key_state(env, args.anthropic)
    stages = build_plan((ROOT / PDF).exists())
    if args.only:
        stages = [s for s in stages if s.name in args.only]
    stages = [s for s in stages if s.name not in args.skip]

    print(f"BOSON_API_KEY: {'set' if keys['boson'] else 'missing'}; ANTHROPIC_API_KEY: "
          f"{'set' if env.get('ANTHROPIC_API_KEY') else 'missing'} ({'allowed' if args.anthropic else 'not allowed without --anthropic'})")
    if not (ROOT / PDF).exists():
        print(f"note: {PDF} is missing, so the ingest stage is not in the plan")
    print()
    runnable: list[Stage] = []
    for s in stages:
        why = blocked_reason(s, keys, env)
        print(f"  {'BLOCKED' if why else 'run    '}  {s.name:<20} {s.why}" + (f"   ({why})" if why else ""))
        if not why:
            runnable.append(s)
    if args.plan:
        return 0
    print()

    for s in runnable:
        print(f"=== {s.name}: {s.why}", flush=True)
        if subprocess.run(command_for(s, args.yes), cwd=ROOT).returncode != 0:
            print(f"\nStopped: stage {s.name} failed. Fix it, then rerun (finished stages are safe to repeat).")
            return 1
        if s.pause_after and not args.yes:
            input(f"\n{s.pause_after}\nPress Enter to continue, or Ctrl+C to stop here. ")
    print("\nAll runnable stages finished. Blocked stages (if any) are listed above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
