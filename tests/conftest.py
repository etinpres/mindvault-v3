"""tests/conftest.py — v3.2.7 test isolation 강제.

배경: src/* 모듈들의 module-level path constant (DEBUG_LOG, DATA_DIR, CACHE_DB 등)
가 ~/.claude/mindvault-v3/ 등 production 경로를 hardcoded 하던 패턴.
테스트가 일부 path 만 patch (MEMORY_DIR, STAGED_DIR) 하고 DEBUG_LOG 등은 누락하면
production 사이드이펙트 발생. v3.2.6 sweep 에서 debug.log 에 가짜 "disk full" 7건
박힌 사례 확인 (test_procedural_slot.py:355 mock OSError → _debug → production log).

Fix: src 모듈들이 MV3_DATA_DIR / MV3_PROJECTS_ROOT / MV3_HOOKS_DIR env var 우선
참조하도록 v3.2.7 에서 변경. 본 conftest.py 가 pytest collection 보다 먼저
(top-level) env var 를 tmp dir 로 강제 — 사용자 production env export 여부와
무관하게 격리.

회귀 테스트: tests/test_path_isolation.py 가 production debug.log 가 변경되지
않음을 verify.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

# v3.2.7: sys.path / sys.modules 격리 — 다른 테스트가 production deploy 경로
# (~/.claude/scripts/mindvault, ~/.claude/hooks) 를 sys.path 에 insert 해서
# worktree 본 import 가 캐싱 미스로 production 본을 잡는 패턴 방지.
# 메모리 [[feedback-test-production-path-pollution]] 참조.
_WORKTREE_ROOT = Path(__file__).resolve().parent.parent
_WORKTREE_SRC = _WORKTREE_ROOT / "src"
_WORKTREE_HOOKS = _WORKTREE_ROOT / "hooks"

# production deploy 경로 제거 (다른 conftest / pytest plugin / test 가 이미 추가했을 수 있음)
sys.path[:] = [
    p for p in sys.path
    if not (p.rstrip("/").endswith("/.claude/scripts/mindvault")
            or p.rstrip("/").endswith("/.claude/hooks"))
]
# worktree src 를 최우선 — 이후 모든 `import session_memory_end` 등이 worktree 본 잡음
for _p in (_WORKTREE_HOOKS, _WORKTREE_SRC):
    while str(_p) in sys.path:
        sys.path.remove(str(_p))
    sys.path.insert(0, str(_p))

# 이미 캐싱된 mindvault 모듈을 제거 — 다음 import 가 새 worktree 본 잡도록
_MV3_MODULES = (
    "session_memory_end", "session_memory", "memory_search", "memory_indexer",
    "memory_extractor", "memory_compiler", "memory_review_cli",
    "extractor_cache", "extractor_stats_cli", "query_intent", "turns_cache",
    "backfill_cli", "dedup_cli", "alias_generator", "sources_cli",
    "eval_top3_domain", "eval_arctic_ko_ab", "self_eval", "search",
    "indexer", "recall_cli", "compiler_benchmark",
    "memory-recall", "session-memory-end", "session-memory",
)
for _name in _MV3_MODULES:
    sys.modules.pop(_name, None)

_TMP_ROOT = Path(tempfile.mkdtemp(prefix="mv3-pytest-isolation-"))

# 강제 override — 사용자 환경에 production export 가 있어도 격리.
# pytest 가 conftest.py 를 collection 보다 먼저 실행하므로, src 모듈의
# module-level Path constant evaluation 보다 앞선다 (import 시점 fix).
os.environ["MV3_DATA_DIR"] = str(_TMP_ROOT / "data")
os.environ["MV3_PROJECTS_ROOT"] = str(_TMP_ROOT / "projects")
os.environ["MV3_HOOKS_DIR"] = str(_TMP_ROOT / "hooks")
os.environ["MV3_SCRIPTS_DIR"] = str(_TMP_ROOT / "scripts")
# v3.4 (T4+): contradiction_detector / contradiction_review_cli 가 참조하는
# runtime dir (debug.log, contradictions.jsonl). 격리 안 하면 테스트가
# production ~/.claude/mindvault-v3/contradictions.jsonl 에 append 함.
os.environ["MV3_RUNTIME_DIR"] = str(_TMP_ROOT / "runtime")

for _p in (
    Path(os.environ["MV3_DATA_DIR"]),
    Path(os.environ["MV3_PROJECTS_ROOT"]),
    Path(os.environ["MV3_HOOKS_DIR"]),
    Path(os.environ["MV3_SCRIPTS_DIR"]),
    Path(os.environ["MV3_RUNTIME_DIR"]),
):
    _p.mkdir(parents=True, exist_ok=True)


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    """pytest 세션 종료 시 tmp dir 정리."""
    try:
        shutil.rmtree(_TMP_ROOT, ignore_errors=True)
    except Exception:
        pass


@pytest.fixture
def write_memory():
    """memory/*.md 파일을 frontmatter+body 로 작성하는 헬퍼.

    Usage:
        def test_x(tmp_path, write_memory):
            p = write_memory(tmp_path, "foo.md", "name: foo\\ntype: feedback", "본문")
    """
    def _write(mem_dir, fname, frontmatter, body):
        p = mem_dir / fname
        p.write_text(f"---\n{frontmatter}\n---\n\n{body}\n", encoding="utf-8")
        return p
    return _write
