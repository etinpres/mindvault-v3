#!/usr/bin/env python3
"""MindVault v2 Sprint 3 — SessionEnd 훅.

세션 종료 시 마지막 턴에 '영구 기억' 트리거가 있으면 Gemma로 후보 추출 →
memory/_staged/*.md 로 저장. 실제 memory/ 파일은 절대 건드리지 않는다.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

# import path 보강: hook (~/.claude/hooks/)에 배포돼도 memory_extractor는
# ~/.claude/scripts/mindvault/에 있음. dev/repo는 src/.
_HOOK_FILE = Path(__file__).resolve()
for _p in (
    Path("/Users/yonghaekim/.claude/scripts/mindvault"),  # production
    _HOOK_FILE.parent,                                     # dev/repo
):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

RECURSION_GUARD_ENV = "MV2_HOOK_RECURSION_GUARD"
# sub-session의 SessionEnd 즉시 skip
if os.environ.get(RECURSION_GUARD_ENV) == "1":
    sys.exit(0)

from memory_extractor import extract_from_jsonl  # type: ignore  # noqa: E402

PROJECTS_DIR = Path("/Users/yonghaekim/.claude/projects/-Users-yonghaekim-my-folder")
MEMORY_DIR = PROJECTS_DIR / "memory"
STAGED_DIR = MEMORY_DIR / "_staged"
DEBUG_LOG = Path("/Users/yonghaekim/.claude/mindvault-v2/debug.log")


def _debug(msg: str) -> None:
    try:
        DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DEBUG_LOG.open("a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] session-end: {msg}\n")
    except Exception:
        pass


def slugify(title: str) -> str:
    slug = re.sub(r"\s+", "_", title.strip())
    slug = re.sub(r"[^\w가-힣\-]", "", slug)
    return slug[:30] or "memory"


def existing_slugs() -> set[str]:
    slugs: set[str] = set()
    for d in (MEMORY_DIR, STAGED_DIR):
        if not d.is_dir():
            continue
        for f in d.glob("*.md"):
            slugs.add(f.stem.split("_", 2)[-1] if "_" in f.stem else f.stem)
    return slugs


def write_staged(item: dict, session_id: str) -> Path | None:
    STAGED_DIR.mkdir(parents=True, exist_ok=True)
    slug = slugify(item["title"])
    ts = time.strftime("%Y%m%d-%H%M%S")
    filename = f"{ts}_{item['type']}_{slug}.md"
    path = STAGED_DIR / filename
    frontmatter = (
        "---\n"
        f"name: {item['title']}\n"
        f"description: {item['title']}\n"
        f"type: {item['type']}\n"
        f"staged_at: {time.strftime('%Y-%m-%dT%H:%M:%S')}\n"
        f"staged_from_session: {session_id[:8]}\n"
        f"reason: {item['reason']}\n"
        f"evidence: {item['evidence']}\n"
        "---\n\n"
        f"{item['body']}\n"
    )
    try:
        path.write_text(frontmatter, encoding="utf-8")
        return path
    except OSError as e:
        _debug(f"write fail {filename}: {e}")
        return None


def main() -> int:
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
        sid = os.environ.get("CLAUDE_SESSION_ID", "")
        if raw:
            try:
                d = json.loads(raw)
                sid = d.get("sessionId") or d.get("session_id") or sid
            except json.JSONDecodeError:
                pass
        if not sid:
            _debug("no session id; skip")
            return 0

        jsonl = PROJECTS_DIR / f"{sid}.jsonl"
        if not jsonl.is_file():
            _debug(f"jsonl missing for {sid[:8]}")
            return 0

        candidates = extract_from_jsonl(jsonl)
        if not candidates:
            _debug(f"no candidates for {sid[:8]}")
            return 0

        slugs = existing_slugs()
        written = 0
        for item in candidates:
            s = slugify(item["title"])
            if s in slugs:
                _debug(f"dup slug {s}, skip")
                continue
            if write_staged(item, sid):
                written += 1
                slugs.add(s)
        _debug(f"session {sid[:8]}: staged {written}/{len(candidates)}")
        return 0
    except Exception as e:
        _debug(f"FATAL {e}\n{traceback.format_exc()}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
