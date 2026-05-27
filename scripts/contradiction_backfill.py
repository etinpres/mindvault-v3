"""One-shot backfill: pair every memory/*.md against every other,
queue contradictions for review.

USAGE
    python scripts/contradiction_backfill.py
    python scripts/contradiction_backfill.py --memory-dir /custom/path
    python scripts/contradiction_backfill.py --dry-run   # show stats, don't append
    python scripts/contradiction_backfill.py --limit 10  # process first N files

This script calls Gemma ~N*top_k times where N is the memory file count.
At ~3s per Gemma call, expect ~10 minutes per 200 files.

After running, review the queue:
    python -m src.contradiction_review_cli list

Apply resolutions interactively:
    python -m src.contradiction_review_cli show <idx>
    python -m src.contradiction_review_cli resolve <idx> --action <action> --apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _add_repo_to_path() -> None:
    """Allow `from contradiction_detector import ...` when running from repo root."""
    repo = Path(__file__).resolve().parent.parent
    src = repo / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))


def _default_memory_dir() -> Path:
    return Path.home() / ".claude" / "projects" / "-Users-yonghaekim" / "memory"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="contradiction_backfill")
    parser.add_argument(
        "--memory-dir",
        type=Path,
        default=None,
        help="Memory directory to sweep (default: ~/.claude/projects/-Users-yonghaekim/memory/)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print contradiction counts but do NOT append to review queue.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process only the first N memory files (for testing).",
    )
    args = parser.parse_args(argv)

    _add_repo_to_path()
    from contradiction_detector import (
        detect_contradictions,
        append_to_review_queue,
    )

    mem_dir = args.memory_dir if args.memory_dir else _default_memory_dir()
    if not mem_dir.exists():
        print(f"memory dir not found: {mem_dir}", file=sys.stderr)
        return 1

    files = sorted(mem_dir.glob("*.md"))
    # Skip MEMORY.md (index, no frontmatter)
    files = [f for f in files if f.name != "MEMORY.md"]
    if args.limit:
        files = files[: args.limit]

    print(f"sweep target: {len(files)} memory files in {mem_dir}")
    if args.dry_run:
        print("(dry-run — no queue append)")

    total_contradictions = 0
    for i, path in enumerate(files, 1):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as e:
            print(f"[{i}/{len(files)}] {path.name}: read error: {e}", file=sys.stderr)
            continue

        # Extract slug + body. Treat each existing memory as the "new" candidate
        # paired against the rest.
        candidate = {
            "slug": path.stem,
            "title": path.stem.replace("_", " ").replace("-", " "),
            "body": text,
            "path": path,
        }

        contradictions = detect_contradictions(candidate, mem_dir)
        if contradictions:
            total_contradictions += len(contradictions)
            print(
                f"[{i}/{len(files)}] {path.name}: "
                f"{len(contradictions)} contradiction(s)"
            )
            if not args.dry_run:
                append_to_review_queue(
                    path.stem, contradictions, new_path=path
                )

    print()
    print(f"=== Total: {total_contradictions} contradictions across {len(files)} files ===")
    if not args.dry_run and total_contradictions > 0:
        print("Review:  python -m src.contradiction_review_cli list")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
