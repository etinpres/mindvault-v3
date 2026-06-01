#!/usr/bin/env python3
"""MindVault v3 — 배포 drift 백스톱 (SessionStart 훅).

repo `src/*.py` 와 배포본 `~/.claude/scripts/mindvault/*.py` 의 sha256 을 비교해,
repo 가 배포본보다 앞서 있는데 배포가 안 된 (= stale 배포) 상태를 세션 시작 시
경고한다. 이것이 바로 2026-05-28→06-01 contradiction 오탐의 근본 원인이었다:
프롬프트 보정 커밋이 repo 에는 있었으나 배포 경로에는 3일간 미반영이었다.

git post-commit/post-merge 훅이 정상 동작하면 drift 는 거의 발생하지 않지만,
훅 우회(`git commit --no-verify`)·외부 동기화 누락·수동 편집 시 stale 배포가
생길 수 있어 마지막 안전망으로 둔다.

스코프: `src/*.py` ↔ `scripts/mindvault/*.py` 동일 basename 비교만 한다. hook/
skill 은 배포 시 rename(예: session_memory.py→session-memory.py) 되고 변경 빈도가
낮아 제외 — 코드 회귀(이번 사건 유형)는 전부 src/*.py 경로라 이 스코프로 충분하다.

출력: drift 있으면 SessionStart `additionalContext`(JSON) 한 줄 경고. 없으면 무출력.
항상 exit 0 (세션을 절대 차단하지 않는다). repo 경로(`.repo-path`) 부재 시 — 즉
repo 없는 엔드유저(curl|bash) 설치 — 조용히 skip.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path


def _runtime_dir() -> Path:
    """런타임 데이터 디렉토리. contradiction_detector._runtime_dir 와 동일 규약."""
    env = os.environ.get("MV3_RUNTIME_DIR")
    if env:
        return Path(env)
    return Path.home() / ".claude" / "mindvault-v3"


def _deployed_dir() -> Path:
    return Path.home() / ".claude" / "scripts" / "mindvault"


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def find_drift() -> list[str]:
    """repo src/ 와 배포본이 다른 *.py 파일 basename 목록 (sorted)."""
    repo_path_file = _runtime_dir() / ".repo-path"
    try:
        repo = Path(repo_path_file.read_text(encoding="utf-8").strip())
    except OSError:
        return []  # repo 경로 없음 → 엔드유저 설치, 비교 대상 없음
    src_dir = repo / "src"
    deployed_dir = _deployed_dir()
    if not src_dir.is_dir() or not deployed_dir.is_dir():
        return []

    drifted: list[str] = []
    for dep in deployed_dir.glob("*.py"):
        src = src_dir / dep.name
        if not src.exists():
            continue  # 배포본에만 있는 파일(deploy_drift_check 자신 등)은 비교 안 함
        try:
            if _sha(src) != _sha(dep):
                drifted.append(dep.name)
        except OSError:
            continue
    return sorted(drifted)


def _warning(drifted: list[str]) -> str:
    repo = ""
    try:
        repo = (_runtime_dir() / ".repo-path").read_text(encoding="utf-8").strip()
    except OSError:
        pass
    n = len(drifted)
    shown = ", ".join(drifted[:5]) + (f" 외 {n - 5}건" if n > 5 else "")
    remedy = f"cd {repo} && MV3_SYNC_ONLY=1 ./install.sh" if repo else "./install.sh 재실행"
    return (
        f"⚠ MindVault 배포 drift 감지: repo src/ 가 배포본(~/.claude/scripts/mindvault)"
        f"보다 앞섭니다 ({n}개: {shown}). 커밋이 미배포 상태일 수 있어 hook 이 옛 코드로"
        f" 동작 중일 위험이 있습니다. 해결: {remedy}"
    )


def main() -> int:
    try:
        drifted = find_drift()
    except Exception:
        return 0  # 백스톱은 어떤 이유로도 세션을 깨지 않는다
    if not drifted:
        return 0
    out = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": _warning(drifted),
        }
    }
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
