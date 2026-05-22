#!/usr/bin/env python3
"""MindVault v2 Sprint 4 — UserPromptSubmit hook.

매 사용자 메시지마다 memory/*.md hybrid 검색 결과를 system-reminder로 주입.
모든 실패는 silent → exit 0 빈 출력. 사용자 메시지 처리 절대 블로킹 X.
"""
from __future__ import annotations

import json
import signal
import subprocess
import sys
import time
from pathlib import Path

DATA_DIR = Path("/Users/yonghaekim/.claude/mindvault-v2")
DEBUG_LOG = DATA_DIR / "debug.log"
METRICS_LOG = DATA_DIR / "metrics.jsonl"
MIN_PROMPT_LEN = 3
# 200ms target. 250 cap to absorb cold-start variance (mlx 첫 forward + sqlite
# open + 104 .md mtime stat). Warm 평균 ~130ms.
HARD_TIMEOUT_MS = 250
SCORE_THRESHOLD = 0.65
TOP_K = 3
MEMORY_DIRS = [
    Path("/Users/yonghaekim/.claude/projects/-Users-yonghaekim/memory"),
    Path("/Users/yonghaekim/.claude/projects/-Users-yonghaekim-my-folder/memory"),
]
INDEX_DB = DATA_DIR / "index.db"

# import 경로 — production(배포본) + dev(repo) 둘 다 지원
_HOOK_FILE = Path(__file__).resolve()
SCRIPTS_DIRS = [
    Path("/Users/yonghaekim/.claude/scripts/mindvault"),
    _HOOK_FILE.parent.parent / "src",
]


def _debug(msg: str) -> None:
    try:
        DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DEBUG_LOG.open("a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] hook-recall: {msg}\n")
    except Exception:
        pass


def _metric(payload: dict) -> None:
    try:
        with METRICS_LOG.open("a") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _mtime_changed() -> bool:
    try:
        db_mt = INDEX_DB.stat().st_mtime
    except FileNotFoundError:
        return True
    for d in MEMORY_DIRS:
        if not d.is_dir():
            continue
        try:
            if d.stat().st_mtime > db_mt:
                return True
            for p in d.glob("*.md"):
                if p.stat().st_mtime > db_mt:
                    return True
        except OSError:
            continue
    return False


def _spawn_reindex() -> None:
    """incremental_index를 백그라운드로 분리 spawn. 결과 안 기다림."""
    try:
        scripts_path = ":".join(str(d) for d in SCRIPTS_DIRS if d.is_dir())
        code = (
            "import sys, os;"
            f"sys.path[:0] = os.environ.get('MV2_SCRIPTS_PATH','').split(':');"
            "from memory_indexer import incremental_index;"
            "incremental_index()"
        )
        env = {"MV2_SCRIPTS_PATH": scripts_path, "PATH": ""}
        # 부모 환경 변수 보존
        import os
        env.update({k: v for k, v in os.environ.items() if k not in env})
        env["MV2_SCRIPTS_PATH"] = scripts_path
        subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
    except Exception as e:
        _debug(f"spawn reindex fail: {e}")


class _Timeout(Exception):
    pass


def _alarm_handler(_signum, _frame):
    raise _Timeout()


def _format_output(results: list[dict]) -> str:
    lines = ["<system-reminder>", "# 메모리 회수 (Layer 4 hybrid)"]
    for r in results:
        srcs = "+".join(r.get("source") or [])
        name = r.get("name") or "(unnamed)"
        desc = r.get("description") or ""
        snippet = r.get("snippet") or ""
        score = r.get("score", 0)
        lines.append(f"- **{name}** (score {score:.2f}, {srcs}) — {desc}")
        if snippet:
            lines.append(f"  발췌: {snippet}")
    lines.append("</system-reminder>")
    return "\n".join(lines) + "\n"


def main() -> int:
    t0 = time.time()
    signal.signal(signal.SIGALRM, _alarm_handler)
    signal.setitimer(signal.ITIMER_REAL, HARD_TIMEOUT_MS / 1000.0)

    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return 0
        if not isinstance(payload, dict):
            return 0

        prompt = (payload.get("prompt") or "").strip()
        if len(prompt) < MIN_PROMPT_LEN:
            return 0

        if _mtime_changed():
            _spawn_reindex()

        for d in SCRIPTS_DIRS:
            if d.is_dir() and str(d) not in sys.path:
                sys.path.insert(0, str(d))

        from memory_search import recall_memory  # noqa: WPS433

        results = recall_memory(
            prompt, top_k=TOP_K, score_threshold=SCORE_THRESHOLD
        )

        elapsed_ms = int((time.time() - t0) * 1000)
        max_score = results[0]["score"] if results else 0.0
        _metric({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "kind": "recall",
            "query_len": len(prompt),
            "elapsed_ms": elapsed_ms,
            "picked": len(results),
            "max_score": max_score,
        })
        _debug(
            f"query_len={len(prompt)} picked={len(results)} elapsed_ms={elapsed_ms}"
        )

        if not results:
            return 0

        sys.stdout.write(_format_output(results))
        return 0
    except _Timeout:
        _debug(f"timeout {HARD_TIMEOUT_MS}ms — skip")
        return 0
    except Exception as e:
        _debug(f"FATAL {type(e).__name__}: {e}")
        return 0
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)


if __name__ == "__main__":
    sys.exit(main())
