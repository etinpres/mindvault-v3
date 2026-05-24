#!/usr/bin/env python3
"""MindVault v3 Sprint 4 — UserPromptSubmit hook.

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

DATA_DIR = Path("~/.claude/mindvault-v3").expanduser()
DEBUG_LOG = DATA_DIR / "debug.log"
METRICS_LOG = DATA_DIR / "metrics.jsonl"
MIN_PROMPT_LEN = 4  # 너무 짧은 키워드는 skip. 잡담은 raw cosine 게이트가 차단.
HARD_TIMEOUT_MS = 400
SCORE_THRESHOLD = 0.65
TOP_K = 1  # 절대 우수한 1건만. 매번 3건 회수는 V1 토큰 낭비 패턴.
RAW_COSINE_MIN_DEFAULT = 0.40  # Sprint 9 Arctic-ko 재튜닝 (도메인 0.44~0.61 vs 잡담 0.23~0.34)
RAW_COSINE_MIN_HINTED = 0.32   # Sprint 9 Arctic-ko 분포 비례 완화

# 회수 의도 명확 키워드 (있으면 임계값 ↓)
RECALL_HINTS = ("예전에", "그때", "이전에", "지난번", "어제", "전에", "옛날에", "저번에")
# Claude Code 가 cwd 마다 별도 projects 슬롯을 만들기 때문에 런타임 glob 으로
# 모든 슬롯의 memory 디렉토리를 흡수한다. (~/.claude/projects/*/memory)
def _discover_memory_dirs() -> list[Path]:
    root = Path("~/.claude/projects").expanduser()
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob("*/memory") if p.is_dir())


MEMORY_DIRS = _discover_memory_dirs()
# Sprint 11: env var `MV3_EXTRA_MEMORY_DIRS=path1:path2` — _mtime_changed가 이
# 디렉토리들도 watch해야 indexer trigger 일관. _spawn_reindex가 부모 env 보존하므로
# indexer 본체는 자체적으로 같은 env 읽어 처리.
import os as _os_envread
_seen_dirs = {str(d) for d in MEMORY_DIRS}
_extra = _os_envread.environ.get("MV3_EXTRA_MEMORY_DIRS", "").strip()
if _extra:
    for _piece in _extra.split(":"):
        _piece = _piece.strip()
        if _piece:
            _p = Path(_piece).expanduser()
            if str(_p) not in _seen_dirs:
                _seen_dirs.add(str(_p))
                MEMORY_DIRS.append(_p)
# Sprint 16: sources.json (영구 등록) 도 mtime watch 대상에 포함
_SOURCES_CFG = DATA_DIR / "sources.json"
try:
    if _SOURCES_CFG.is_file():
        import json as _json
        _cfg = _json.loads(_SOURCES_CFG.read_text(encoding="utf-8"))
        for _s in (_cfg.get("sources") or []):
            if isinstance(_s, str) and _s:
                _p = Path(_s).expanduser()
                if str(_p) not in _seen_dirs:
                    _seen_dirs.add(str(_p))
                    MEMORY_DIRS.append(_p)
except Exception:
    pass
INDEX_DB = DATA_DIR / "index.db"

# import 경로 — production(배포본) + dev(repo) 둘 다 지원
_HOOK_FILE = Path(__file__).resolve()
SCRIPTS_DIRS = [
    Path("~/.claude/scripts/mindvault").expanduser(),
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
            # Sprint 13: _procedural/ 하위 .md 변경도 reindex trigger
            proc = d / "_procedural"
            if proc.is_dir():
                if proc.stat().st_mtime > db_mt:
                    return True
                for p in proc.glob("*.md"):
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
            f"sys.path[:0] = os.environ.get('MV3_SCRIPTS_PATH','').split(':');"
            "from memory_indexer import incremental_index;"
            "incremental_index()"
        )
        env = {"MV3_SCRIPTS_PATH": scripts_path, "PATH": ""}
        # 부모 환경 변수 보존
        import os
        env.update({k: v for k, v in os.environ.items() if k not in env})
        env["MV3_SCRIPTS_PATH"] = scripts_path
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


RECURSION_GUARD_ENV = "MV3_HOOK_RECURSION_GUARD"


def main() -> int:
    # sub-session의 hook 즉시 skip (자기 자신 발동에서 또 호출되는 무한 재귀 차단)
    import os as _os
    if _os.environ.get(RECURSION_GUARD_ENV) == "1":
        try:
            sys.stdin.read()
        except Exception:
            pass
        return 0
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

        # Sprint 16: query intent classifier — chat/meta 는 회수 강제 skip.
        # rule-based 라 latency 추가 ~0. 미import 실패 시 fallback (skip 없음).
        intent_label = "unknown"
        intent_match: list[str] = []
        try:
            from query_intent import (  # noqa: WPS433
                classify,
                classify_with_gemma,
                gemma_intent_enabled,
                should_skip_recall,
            )
            intent_obj = classify(prompt)
            # Sprint NEXT-3: rule-based unknown 보강 — opt-in env 일 때만, 짧은 query 만.
            if intent_obj.intent == "unknown" and gemma_intent_enabled():
                gemma_obj = classify_with_gemma(prompt)
                if gemma_obj is not None:
                    intent_obj = gemma_obj
            intent_label = intent_obj.intent
            intent_match = list(intent_obj.matched)
            if should_skip_recall(intent_obj):
                _metric({
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "kind": "recall_skip",
                    "reason": f"intent:{intent_label}",
                    "intent": intent_label,
                    "matched": intent_match,
                    "query_len": len(prompt),
                })
                _debug(
                    f"skip recall intent={intent_label} match={intent_match!r}"
                )
                return 0
        except Exception as e:
            _debug(f"intent classify skipped: {type(e).__name__}: {e}")

        from memory_search import recall_memory  # noqa: WPS433

        # 회수 단서어 있으면 임계값 완화 (사용자 의도 명확)
        has_hint = (
            intent_label == "recall"
            or any(h in prompt for h in RECALL_HINTS)
        )
        raw_min = RAW_COSINE_MIN_HINTED if has_hint else RAW_COSINE_MIN_DEFAULT

        results = recall_memory(
            prompt,
            top_k=TOP_K,
            score_threshold=SCORE_THRESHOLD,
            raw_cosine_min=raw_min,
        )

        elapsed_ms = int((time.time() - t0) * 1000)
        max_score = results[0]["score"] if results else 0.0
        raw_top = results[0].get("raw_cosine", 0.0) if results else 0.0
        _metric({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "kind": "recall",
            "query_len": len(prompt),
            "elapsed_ms": elapsed_ms,
            "picked": len(results),
            "max_score": max_score,
            "raw_top1_cosine": raw_top,
            "raw_min": raw_min,
            "has_hint": has_hint,
            "intent": intent_label,
            "intent_matched": intent_match[:3],
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
