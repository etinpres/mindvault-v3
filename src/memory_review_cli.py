#!/usr/bin/env python3
"""MindVault v2 Sprint 3 — /memory review CLI.

하위 명령:
  list                     → staged 후보 JSON 출력
  approve <filename>       → staged → memory/ 이동 + MEMORY.md 한 줄 append
  reject  <filename>       → staged 파일 삭제
  prune                    → 30일 경과 staged 삭제
"""
from __future__ import annotations

import json
import re
import sys
import time
import traceback
from pathlib import Path

PROJECTS_DIR = Path("/Users/yonghaekim/.claude/projects/-Users-yonghaekim-my-folder")
MEMORY_DIR = PROJECTS_DIR / "memory"
STAGED_DIR = MEMORY_DIR / "_staged"
INDEX_MD = MEMORY_DIR / "MEMORY.md"
DEBUG_LOG = Path("/Users/yonghaekim/.claude/mindvault-v2/debug.log")
STAGED_TTL_DAYS = 30


def _debug(msg: str) -> None:
    try:
        DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DEBUG_LOG.open("a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] review: {msg}\n")
    except Exception:
        pass


def parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    fm_raw = parts[1]
    body = parts[2].lstrip("\n")
    meta = {}
    for line in fm_raw.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta, body


def cmd_list() -> int:
    if not STAGED_DIR.is_dir():
        sys.stdout.write(json.dumps({"staged": []}, ensure_ascii=False))
        return 0
    items = []
    now = time.time()
    for f in sorted(STAGED_DIR.glob("*.md")):
        try:
            text = f.read_text(encoding="utf-8")
            meta, body = parse_frontmatter(text)
            age_days = int((now - f.stat().st_mtime) // 86400)
            items.append(
                {
                    "file": f.name,
                    "type": meta.get("type", "feedback"),
                    "title": meta.get("name", f.stem),
                    "body": body.strip(),
                    "reason": meta.get("reason", ""),
                    "evidence": meta.get("evidence", ""),
                    "staged_at": meta.get("staged_at", ""),
                    "age_days": age_days,
                }
            )
        except OSError as e:
            _debug(f"list read fail {f.name}: {e}")
    sys.stdout.write(json.dumps({"staged": items}, ensure_ascii=False))
    return 0


def _promoted_slug(staged_name: str) -> str:
    # 20260415-120000_feedback_no_mocks.md → no_mocks
    stem = Path(staged_name).stem
    m = re.match(r"\d{8}-\d{6}_[a-z]+_(.+)$", stem)
    return m.group(1) if m else stem


def _safe_staged_path(filename: str) -> Path | None:
    """filename이 STAGED_DIR 내부의 단일 md 파일인지 검증. path traversal 차단."""
    if not filename or filename != Path(filename).name or not filename.endswith(".md"):
        return None
    return STAGED_DIR / filename


def cmd_approve(filename: str) -> int:
    src = _safe_staged_path(filename)
    if src is None:
        sys.stdout.write(json.dumps({"ok": False, "error": "invalid filename"}))
        return 0
    if not src.is_file():
        sys.stdout.write(json.dumps({"ok": False, "error": "not found"}))
        return 0
    try:
        text = src.read_text(encoding="utf-8")
        meta, body = parse_frontmatter(text)
        slug = _promoted_slug(filename)
        target = MEMORY_DIR / f"{slug}.md"
        if target.exists():
            sys.stdout.write(json.dumps({"ok": False, "error": "target exists", "target": str(target)}))
            return 0

        final_fm = (
            "---\n"
            f"name: {meta.get('name', slug)}\n"
            f"description: {meta.get('description', meta.get('name', slug))}\n"
            f"type: {meta.get('type', 'feedback')}\n"
            "---\n\n"
            f"{body.rstrip()}\n"
        )
        target.write_text(final_fm, encoding="utf-8")

        if INDEX_MD.is_file():
            line = f"- [{meta.get('name', slug)}]({slug}.md) — {meta.get('reason', '')}\n"
            existing = INDEX_MD.read_text(encoding="utf-8")
            prefix = "" if existing.endswith("\n") else "\n"
            with INDEX_MD.open("a", encoding="utf-8") as f:
                f.write(prefix + line)

        src.unlink()

        # Sprint 4: 새 메모리 즉시 임베딩 인덱싱 (실패해도 staged 작업은 성공)
        reindex_info: dict = {}
        try:
            from memory_indexer import incremental_index  # noqa: WPS433
            reindex_info = incremental_index()
        except Exception as e:
            _debug(f"approve reindex skip: {type(e).__name__}: {e}")
            reindex_info = {"skipped": "reindex failed", "error": str(e)}

        sys.stdout.write(json.dumps(
            {"ok": True, "target": str(target), "reindex": reindex_info},
            ensure_ascii=False,
        ))
        return 0
    except Exception as e:
        _debug(f"approve FATAL {filename}: {e}")
        sys.stdout.write(json.dumps({"ok": False, "error": str(e)}))
        return 0


def cmd_reject(filename: str) -> int:
    src = _safe_staged_path(filename)
    if src is None:
        sys.stdout.write(json.dumps({"ok": False, "error": "invalid filename"}))
        return 0
    if not src.is_file():
        sys.stdout.write(json.dumps({"ok": False, "error": "not found"}))
        return 0
    try:
        src.unlink()
        sys.stdout.write(json.dumps({"ok": True}))
        return 0
    except OSError as e:
        sys.stdout.write(json.dumps({"ok": False, "error": str(e)}))
        return 0


def cmd_prune() -> int:
    if not STAGED_DIR.is_dir():
        sys.stdout.write(json.dumps({"removed": 0}))
        return 0
    cutoff = time.time() - STAGED_TTL_DAYS * 86400
    removed = 0
    for f in STAGED_DIR.glob("*.md"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
                removed += 1
        except OSError:
            continue
    sys.stdout.write(json.dumps({"removed": removed}))
    return 0


def main() -> int:
    try:
        if len(sys.argv) < 2:
            sys.stdout.write(json.dumps({"error": "usage: list|approve|reject|prune"}))
            return 0
        sub = sys.argv[1]
        if sub == "list":
            return cmd_list()
        if sub == "approve" and len(sys.argv) >= 3:
            return cmd_approve(sys.argv[2])
        if sub == "reject" and len(sys.argv) >= 3:
            return cmd_reject(sys.argv[2])
        if sub == "prune":
            return cmd_prune()
        sys.stdout.write(json.dumps({"error": "bad args"}))
        return 0
    except Exception as e:
        _debug(f"main FATAL: {e}\n{traceback.format_exc()}")
        sys.stdout.write(json.dumps({"ok": False, "error": "fatal"}))
        return 0


if __name__ == "__main__":
    sys.exit(main())
