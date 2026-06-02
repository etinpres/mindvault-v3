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
import json
import os
import re
import sys
from pathlib import Path
from typing import List, Optional

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from memory_indexer import parse_frontmatter  # noqa: E402


def _safe_scalar(value: str) -> str:
    """frontmatter 라인 값으로 안전하면 그대로, 아니면 JSON 인용(유효 YAML scalar).

    audit sweep R1: source_ref 는 외부(import 파이프라인) frontmatter 에서 온 임의
    값이라 ': '(콜론+공백)·선행 YAML indicator 가 들어오면 unquoted 로 쓸 때
    yaml.safe_load(recall/indexer 경로)가 **frontmatter 전체**를 버려 provenance·
    reverify 가 silent 소실됐다. plain scalar 로 round-trip 되면(UUID·일반 URL)
    그대로 두어 기존 raw-line 단언 호환, 위험할 때만 json.dumps 로 인용한다.
    """
    try:
        if yaml.safe_load(f"k: {value}") == {"k": value}:
            return value
    except yaml.YAMLError:
        pass
    return json.dumps(value, ensure_ascii=False)


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
    # bug-audit 2026-06-02 (#25): source_ref 가 이미 있으면 재주입 금지. 가드가
    # source_type 부재만 보므로, source_ref 만 있고 source_type 없는 파일(중단된
    # 과거 backfill·수기 편집·구버전 import)에 source_ref 가 2번 들어가 YAML
    # last-key-wins 로 원본 출처가 silent 덮어쓰였다. 기존 ref 는 보존한다.
    if ref and not _has(fm, "source_ref"):
        inject.append(f"source_ref: {_safe_scalar(ref)}")
    new = lines[:close] + inject + lines[close:]
    out = "\n".join(new)
    # 안전 가드(audit sweep R1): 주입 결과가 yaml 로 다시 파싱돼야 한다. 외부 ref 의
    # ': '/indicator 가 frontmatter 전체를 깨면(→ {}) provenance·reverify 가 silent
    # 소실되므로, 파싱 불가가 되면 쓰지 않고 skip(원본 보존). _safe_scalar 가 정상
    # 입력엔 no-op 이라 기존 동작 불변.
    if not parse_frontmatter(out)[0]:
        return False
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

    # bug-audit 2026-06-01 (provenance-missing-dir-silent): 경로 오타/미존재 시 glob 이
    # 조용히 0건 반환 → '0건 성공'(exit 0)으로 오인. backfill 전에 존재 검사로 구분.
    if not Path(a.memory_dir).is_dir():
        ap.error(f"memory_dir not found: {a.memory_dir}")

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
