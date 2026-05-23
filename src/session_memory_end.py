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
# ~/.claude/scripts/mindvault/에 있음. dev/repo는 src/ 옆에 같이 있음.
# 자기 자신이 있는 디렉토리에 memory_extractor.py 가 같이 있으면 dev/repo 또는
# 정상 배포된 production — 그쪽만 sys.path 에 등록한다. 없을 때만(hooks/ 만 배포된 경우)
# production fallback 을 추가. 이렇게 안 하면 worktree 테스트 시 production 코드가
# 우선 잡혀 새 함수가 안 보임.
_HOOK_FILE = Path(__file__).resolve()
_HOOK_DIR = _HOOK_FILE.parent
if (_HOOK_DIR / "memory_extractor.py").is_file():
    if str(_HOOK_DIR) not in sys.path:
        sys.path.insert(0, str(_HOOK_DIR))
else:
    _PROD = Path("/Users/yonghaekim/.claude/scripts/mindvault")
    if _PROD.is_dir() and str(_PROD) not in sys.path:
        sys.path.insert(0, str(_PROD))

RECURSION_GUARD_ENV = "MV2_HOOK_RECURSION_GUARD"
# sub-session의 SessionEnd 즉시 skip
if os.environ.get(RECURSION_GUARD_ENV) == "1":
    sys.exit(0)

from memory_extractor import extract_from_jsonl  # type: ignore  # noqa: E402

PROJECTS_DIR = Path("/Users/yonghaekim/.claude/projects/-Users-yonghaekim-my-folder")
MEMORY_DIR = PROJECTS_DIR / "memory"
STAGED_DIR = MEMORY_DIR / "_staged"
# Sprint 13: procedural type 후보는 _procedural/_staged/ 슬롯에 저장. 결정 메모리와
# 분리해 indexer + grep·인벤토리 시 한눈에 구분 가능. memory_review_cli 가
# 양쪽 staged 모두 스캔.
PROCEDURAL_DIR = MEMORY_DIR / "_procedural"
PROCEDURAL_STAGED_DIR = PROCEDURAL_DIR / "_staged"
DEBUG_LOG = Path("/Users/yonghaekim/.claude/mindvault-v2/debug.log")


def staged_dir_for(memory_type: str) -> Path:
    """type 별 staged 슬롯. procedural 만 _procedural/_staged/, 나머지는 기존 슬롯."""
    if memory_type == "procedural":
        return PROCEDURAL_STAGED_DIR
    return STAGED_DIR


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
    for d in (MEMORY_DIR, STAGED_DIR, PROCEDURAL_DIR, PROCEDURAL_STAGED_DIR):
        if not d.is_dir():
            continue
        for f in d.glob("*.md"):
            slugs.add(f.stem.split("_", 2)[-1] if "_" in f.stem else f.stem)
    return slugs


def write_staged(
    item: dict, session_id: str, slug_override: str | None = None
) -> Path | None:
    """staged 파일 작성. slug_override 로 충돌 회피 suffix 부여 가능 (Sprint NEXT-6)."""
    staged_dir = staged_dir_for(item["type"])
    staged_dir.mkdir(parents=True, exist_ok=True)
    slug = slug_override if slug_override is not None else slugify(item["title"])
    ts = time.strftime("%Y%m%d-%H%M%S")
    filename = f"{ts}_{item['type']}_{slug}.md"
    path = staged_dir / filename
    fm_lines = [
        f"name: {item['title']}",
        f"description: {item['title']}",
        f"type: {item['type']}",
        f"staged_at: {time.strftime('%Y-%m-%dT%H:%M:%S')}",
        f"staged_from_session: {session_id[:8]}",
        f"reason: {item['reason']}",
        f"evidence: {item['evidence']}",
    ]
    # Sprint 14: memory compiler 가 부착한 update 메타 보존. review CLI 가
    # update_of 보고 diff/approve 분기.
    if item.get("update_of"):
        fm_lines.append(f"update_of: {item['update_of']}")
    if item.get("diff_summary"):
        fm_lines.append(f"diff_summary: {item['diff_summary']}")
    frontmatter = "---\n" + "\n".join(fm_lines) + "\n---\n\n" + f"{item['body']}\n"
    try:
        path.write_text(frontmatter, encoding="utf-8")
        return path
    except OSError as e:
        _debug(f"write fail {filename}: {e}")
        return None


def _stage_with_conflict_resolution(
    candidates: list[dict],
    existing_slugs_set: set,
    session_id: str,
    writer,
) -> int:
    """Sprint NEXT-6: session 안 동일 slug 다중 candidate 처리.

    - 기존 memory 와 slug 충돌 + update_of 없음 → skip (file overwrite 방지)
    - session 안 동일 slug + body 완전 동일 → skip (dedup, 정보 손실 아님)
    - session 안 동일 slug + body 다름 → `_2`, `_3` suffix 로 모두 살림
    writer(item, session_id, slug_override=...) -> Path | None 콜백.
    """
    session_slug_bodies: dict[str, list[str]] = {}
    written = 0
    for item in candidates:
        s_base = slugify(item["title"])
        body = (item.get("body") or "").strip()
        if s_base in existing_slugs_set and not item.get("update_of"):
            _debug(f"dup slug vs existing {s_base}, skip")
            continue
        prev_bodies = session_slug_bodies.setdefault(s_base, [])
        if body and body in prev_bodies:
            _debug(f"dup body in session {s_base}, skip")
            continue
        s_final = (
            s_base if not prev_bodies else f"{s_base}_{len(prev_bodies) + 1}"
        )
        prev_bodies.append(body)
        if writer(item, session_id, slug_override=s_final):
            written += 1
            # NOTE: existing_slugs_set 에 추가하지 않음 — session 안 추적은
            # session_slug_bodies 가 담당. existing_slugs_set 는 "file system 의
            # 기존 memory" 만 의미해야, 동일 session 안의 다음 candidate 가
            # 잘못 "기존 충돌" 로 skip 되지 않는다.
    return written


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

        # Sprint 14: opt-in auto compile — 기존 memory 와 매칭되는 후보는
        # Gemma 가 정제해 update_of 메타 부착. env MV2_AUTO_COMPILE=1 일 때만.
        # 정제 실패는 silent — 원본 candidate 그대로 staged 처리.
        try:
            from memory_compiler import auto_compile_enabled, compile_candidates
            if auto_compile_enabled():
                before = sum(1 for c in candidates if not c.get("update_of"))
                candidates = compile_candidates(candidates)
                updates = sum(1 for c in candidates if c.get("update_of"))
                _debug(
                    f"compiled session={sid[:8]} updates={updates}/{before}"
                )
        except Exception as e:
            _debug(f"compile skipped: {type(e).__name__}: {e}")

        slugs = existing_slugs()
        written = _stage_with_conflict_resolution(
            candidates, slugs, sid, write_staged
        )
        _debug(f"session {sid[:8]}: staged {written}/{len(candidates)}")
        return 0
    except Exception as e:
        _debug(f"FATAL {e}\n{traceback.format_exc()}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
