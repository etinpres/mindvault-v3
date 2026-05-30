#!/usr/bin/env python3
"""기존 메모리에 source_type/source_ref 소급 부여. 억측 금지: 기록된 session id가
있으면 session, 없으면 unknown. atomic write (tmp + os.replace).

session id 우선순위:
  1. staged_from_session  (staged-origin 파일)
  2. originSessionId       (top-level, 실제 기록된 세션 UUID)
  3. metadata.originSessionId (nested, memory-production 파이프라인 실제 형식)
"""
from __future__ import annotations
import argparse
import os
import re
import sys
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).parent))
from memory_indexer import parse_frontmatter  # noqa: E402


def _has(fm: dict, key: str) -> bool:
    return key in fm and fm[key] not in (None, "")


def _find_session_ref(fm: dict) -> Optional[str]:
    """Return the first non-empty session id found, in priority order, or None."""
    # Priority 1: staged_from_session
    if _has(fm, "staged_from_session"):
        return str(fm["staged_from_session"])
    # Priority 2: top-level originSessionId
    if _has(fm, "originSessionId"):
        return str(fm["originSessionId"])
    # Priority 3: nested metadata.originSessionId
    meta = fm.get("metadata")
    if isinstance(meta, dict) and meta.get("originSessionId") not in (None, ""):
        return str(meta["originSessionId"])
    return None


def backfill_file(path: Path, dry_run: bool) -> bool:
    """Inject source_type/source_ref into a single memory file.

    Returns True if the file would be (dry_run) or was injected.
    Returns False if skipped (unreadable, no frontmatter, already has source_type,
    or closing fence not locatable — tolerant, never raises ValueError).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    fm, body = parse_frontmatter(text)
    if not fm or _has(fm, "source_type"):
        return False

    lines = text.split("\n")

    # Problem B fix: tolerant close-fence detection (handles trailing whitespace)
    close = next((i for i, l in enumerate(lines[1:], 1) if l.strip() == "---"), None)
    if close is None:
        return False  # Cannot safely locate closing fence — skip

    ref = _find_session_ref(fm)
    if ref:
        # Round-2 fix (Item 2): collapse multi-line YAML block-scalar values so
        # the injected source_ref: line stays on a single line and
        # re-parses cleanly with the line-based frontmatter parser.
        ref = re.sub(r"\s+", " ", str(ref)).strip()
        st = "session"
    else:
        st, ref = "unknown", ""

    inject = [f"source_type: {st}"]
    if ref:
        inject.append(f"source_ref: {ref}")
    new = lines[:close] + inject + lines[close:]
    out = "\n".join(new)
    if dry_run:
        return True
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(out, encoding="utf-8")
        os.replace(tmp, path)
        return True
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _collect_files(d: Path) -> List[Path]:
    """Collect eligible memory files: root/*.md + _procedural/*.md,
    skip MEMORY.md, skip any path that has a '_staged' part.
    Note: symlinks are followed (no symlink-outside guard — out of scope for this
    personal-memory CLI; unlike memory_indexer._collect_md_files which applies a
    _safe_memory_path containment check)."""
    files: List[Path] = []
    for p in d.glob("*.md"):
        if p.name == "MEMORY.md":
            continue
        if any(part == "_staged" for part in p.parts):
            continue
        files.append(p)
    proc_dir = d / "_procedural"
    if proc_dir.is_dir():
        for p in proc_dir.glob("*.md"):
            if p.name == "MEMORY.md":
                continue
            if any(part == "_staged" for part in p.parts):
                continue
            files.append(p)
    return sorted(files)


def backfill_dir(
    d: Path,
    dry_run: bool,
    skipped: Optional[List[str]] = None,
    failed: Optional[List[str]] = None,
) -> int:
    """Process all eligible memory files under d.

    Returns count of files that were (or would be in dry_run) injected.
    Optionally populates `skipped` (no/bad frontmatter) and `failed` (exception)
    lists with file names — callers that need operability reporting pass in lists.

    Backward-compatible: old callers that only use backfill_dir(d, dry_run) -> int
    continue to work unchanged.
    """
    n = 0
    for p in _collect_files(d):
        try:
            result = backfill_file(p, dry_run)
            if result:
                n += 1
            else:
                # backfill_file returned False → skipped (unreadable / no fm / already set / no fence)
                if skipped is not None:
                    skipped.append(p.name)
        except Exception as e:
            # Problem B fix: per-file resilience — one bad file cannot abort the batch
            # Round-2 fix (Item 3): include exception type for operability reporting.
            if failed is not None:
                failed.append(f"{p.name} ({type(e).__name__})")
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("memory_dir")
    ap.add_argument("--apply", action="store_true", help="실제 쓰기 (기본 dry-run)")
    a = ap.parse_args()

    skipped: List[str] = []
    failed: List[str] = []
    n = backfill_dir(Path(a.memory_dir), dry_run=not a.apply, skipped=skipped, failed=failed)

    mode = "적용" if a.apply else "dry-run"
    print(f"{mode}: {n}건")
    if skipped:
        print(f"건너뜀 ({len(skipped)}): {', '.join(skipped)}")
    if failed:
        print(f"오류 ({len(failed)}): {', '.join(failed)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
