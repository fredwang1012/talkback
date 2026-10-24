"""One-off: move the shared voices.json / faces.json / portraits/faces / cast_settings.json to the owner's namespace.

The live app does this itself on first start with PER_USER=true; this script lets you run it explicitly against a
copy or mount of the bucket first. Dry run by default (prints what it would do); --apply copies. Copies only: never
overwrites an existing file and never deletes the originals. Safe to re-run.

  uv run --project backend python scripts/migrate_per_user.py --data-dir <path to data/ or a bucket mount>
  uv run --project backend python scripts/migrate_per_user.py --data-dir <path> --owner you@example.com --apply

Against the bucket directly: gcloud storage rsync -r gs://frederick-talkback-data <local dir>, run with --apply,
then rsync the new <local dir>/users and books/*/owner.txt back up (never with --delete-unmatched-destination-objects).
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app import users  # noqa: E402
from app.config import settings  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, required=True)
    ap.add_argument("--owner", default=(settings.owner_email_list or ["fredwang1012@gmail.com"])[0])
    ap.add_argument("--apply", action="store_true", help="actually copy (default: dry run)")
    args = ap.parse_args()
    if not args.data_dir.is_dir():
        print(f"{args.data_dir} is not a directory", file=sys.stderr)
        return 2
    print(f"owner {args.owner} -> data/users/{users.ns_for(args.owner)}  ({'APPLY' if args.apply else 'dry run'})")
    acts = users.migrate_to_owner(args.data_dir, args.owner, dry_run=not args.apply, say=lambda fmt, *a: print(fmt % a))
    print(f"{len(acts)} action(s)" + ("" if args.apply else "; re-run with --apply to perform them"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
