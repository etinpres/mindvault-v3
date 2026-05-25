#!/usr/bin/env python3
"""MindVault v3 — SessionStart 훅. 최근 N개 세션을 Claude Code(`claude -p`)로 요약해 컨텍스트에 주입.

2026-05-22 변경: Gemma MLX (45초 cache MISS) → `claude -p --model haiku` (10-15초).
recursion guard 환경변수로 sub-session에서 자기 자신 발동 차단.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

def _default_projects_dir() -> Path:
    """현재 사용자 $HOME 으로부터 Claude Code 프로젝트 슬롯 경로 파생.
    예: HOME=/Users/alice → ~/.claude/projects/-Users-alice/.
    `MV3_PROJECTS_DIR` 환경변수로 override 가능.
    """
    override = os.environ.get("MV3_PROJECTS_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    home_slug = "-" + str(Path.home()).strip("/").replace("/", "-")
    return Path(os.environ.get("MV3_PROJECTS_ROOT", "~/.claude/projects")).expanduser() / home_slug


# v3.2.7: production state pollution 방지. MV3_DATA_DIR env var 우선.
_MV3_DATA_DIR = Path(os.environ.get("MV3_DATA_DIR", "~/.claude/mindvault-v3")).expanduser()
PROJECTS_DIR = _default_projects_dir()
CACHE_DIR = _MV3_DATA_DIR / "cache"
DEBUG_LOG = _MV3_DATA_DIR / "debug.log"
SIGNATURE = "# 지난 세션 요약 (MindVault v3)"
RECURSION_GUARD_ENV = "MV3_HOOK_RECURSION_GUARD"
CLAUDE_FALLBACK_PATH = os.path.expanduser("~/.nvm/versions/node/v24.13.0/bin/claude")
CLAUDE_MODEL = "haiku"
CLAUDE_TIMEOUT = 90  # subprocess cap (startup + plugin sync + model 합쳐서 여유)

MAX_SESSIONS = 5
MAX_MSG_CHARS = 400
# legacy aliases — call_gemma signature 호환 위해 유지 (max_tokens는 claude -p에서 무시)
GEMMA_MINI_MAX_TOKENS = 1200
GEMMA_UNIFIED_MAX_TOKENS = 2500
CACHE_DAYS = 7
CACHE_VERSION = "v4-claude-p"  # bump to invalidate Gemma-generated caches

# Per-session turn budget: index 0 = most recent. Earlier sessions get fewer turns.
TURN_WEIGHTS = [
    (12, 12),  # session 1 (most recent) — generous
    (10, 10),
    (8, 8),
    (6, 6),
    (4, 6),    # session 5 (oldest) — tail-focused to catch decisions
]

SECRET_PATTERNS = [
    (re.compile(r"sk-[a-zA-Z0-9_-]{20,}"), "[REDACTED_KEY]"),
    (re.compile(r"ghp_[a-zA-Z0-9]{20,}"), "[REDACTED_KEY]"),
    (re.compile(r"Bearer\s+[a-zA-Z0-9._-]{20,}"), "Bearer [REDACTED]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED_AWS]"),
]


def _debug(msg: str) -> None:
    try:
        DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DEBUG_LOG.open("a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def redact(text: str) -> str:
    for pat, repl in SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return text


def _is_system_reminder(text: str) -> bool:
    """시스템 리마인더 블록인지 판별 (블록 단위)."""
    head = text.lstrip()[:50]
    return head.startswith("<system-reminder>") or head.startswith("<command-")


def extract_text_from_content(content) -> str:
    """user/assistant의 content 필드에서 일반 텍스트 추출. 시스템 리마인더 블록은 블록 단위로 스킵."""
    if isinstance(content, str):
        return "" if _is_system_reminder(content) else content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        text_val = block.get("text")
        if btype == "text" or (btype is None and text_val is not None):
            t = str(text_val or "")
            if _is_system_reminder(t):
                continue
            parts.append(t)
    return "\n".join(p for p in parts if p)


def extract_messages(jsonl_path: Path, head_turns: int = 6, tail_turns: int = 6) -> list[dict]:
    """JSONL에서 (role, text) 메시지만 추출. 첫 head_turns + 마지막 tail_turns."""
    messages: list[dict] = []
    try:
        with jsonl_path.open() as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = d.get("type")
                if t not in ("user", "assistant"):
                    continue
                msg = d.get("message") or {}
                content = msg.get("content")
                text = extract_text_from_content(content).strip()
                if not text:
                    continue
                if SIGNATURE in text:
                    continue
                text = redact(text)
                text = text[:MAX_MSG_CHARS]
                messages.append({"role": t, "text": text})
    except OSError as e:
        _debug(f"JSONL read failed {jsonl_path.name}: {e}")
        return []

    if len(messages) <= head_turns + tail_turns:
        return messages
    return messages[:head_turns] + messages[-tail_turns:]


def get_recent_sessions(exclude_session_id: str | None) -> list[Path]:
    """최근 수정된 JSONL 5개.
    exclude_session_id가 있으면 그 파일 제외.
    없으면 휴리스틱: 가장 최근 mtime 파일 1개를 '현재 세션 추정'으로 제외.
    """
    if not PROJECTS_DIR.is_dir():
        return []
    files = []
    for p in PROJECTS_DIR.glob("*.jsonl"):
        if exclude_session_id and p.stem == exclude_session_id:
            continue
        try:
            files.append((p.stat().st_mtime, p))
        except OSError:
            continue
    files.sort(key=lambda x: x[0], reverse=True)
    if not exclude_session_id and files:
        files = files[1:]  # 휴리스틱: 가장 최근은 현재 세션일 가능성 높음
    return [p for _, p in files[:MAX_SESSIONS]]


def cache_key(paths: list[Path]) -> str:
    parts = [CACHE_VERSION]
    for p in paths:
        try:
            parts.append(f"{p}:{p.stat().st_mtime_ns}")
        except OSError:
            parts.append(str(p))
    return hashlib.sha256("\n".join(sorted(parts)).encode()).hexdigest()


def cache_get(key: str) -> str | None:
    f = CACHE_DIR / f"{key}.txt"
    if f.is_file():
        try:
            return f.read_text()
        except OSError:
            return None
    return None


def cache_set(key: str, value: str) -> None:
    # v3.2.6 Round 2 (LR1): atomic write — parallel SessionStart hook 이 동일
    # key 에 동시 write 시 partial 잔류 회피. tmp + os.replace 패턴.
    # v3.2.8: finally — KeyboardInterrupt 도 tmp orphan 차단. tmp 정의를 try
    # 밖으로 빼서 mkdir 실패 시 UnboundLocalError 회피.
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        _debug(f"cache_set mkdir failed: {e}")
        return
    target = CACHE_DIR / f"{key}.txt"
    tmp = target.with_suffix(".txt.tmp")
    try:
        tmp.write_text(value)
        os.replace(tmp, target)
    except OSError as e:
        _debug(f"cache_set failed: {e}")
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def cache_purge_old() -> None:
    if not CACHE_DIR.is_dir():
        return
    cutoff = time.time() - CACHE_DAYS * 86400
    for f in CACHE_DIR.glob("*.txt"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            continue


def _claude_cmd() -> str:
    """`claude` 실행 경로. PATH 부재 시 nvm 폴백."""
    found = shutil.which("claude")
    if found:
        return found
    if Path(CLAUDE_FALLBACK_PATH).is_file():
        return CLAUDE_FALLBACK_PATH
    return "claude"  # 마지막 폴백 — 실패 시 subprocess가 raise


def call_gemma(prompt: str, max_tokens: int = 2000) -> str | None:
    """Claude Code `claude -p --model haiku` 호출. (legacy 함수명 유지, 호출부 호환)

    sub-session에서 mv2 hook 무한재귀 방지 위해 RECURSION_GUARD_ENV=1 주입.
    max_tokens는 claude -p에 직접 전달 못 하므로 무시 (length는 system-prompt로 가이드).
    """
    # 안전망: 이미 sub-hook 안이면 더 깊은 재귀 차단
    if os.environ.get(RECURSION_GUARD_ENV) == "1":
        _debug("call_gemma skipped — already inside recursion guard")
        return None

    env = os.environ.copy()
    env[RECURSION_GUARD_ENV] = "1"
    # nvm bin 경로를 PATH에 추가 (hook 환경 PATH가 빈약할 때 보강)
    nvm_bin = os.path.expanduser("~/.nvm/versions/node/v24.13.0/bin")
    env["PATH"] = nvm_bin + ":" + env.get("PATH", "/usr/bin:/bin")

    try:
        result = subprocess.run(
            [_claude_cmd(), "-p", "--model", CLAUDE_MODEL, prompt],
            capture_output=True,
            timeout=CLAUDE_TIMEOUT,
            env=env,
            text=True,
        )
    except subprocess.TimeoutExpired:
        _debug(f"claude -p timeout {CLAUDE_TIMEOUT}s")
        return None
    except FileNotFoundError as e:
        _debug(f"claude binary not found: {e}")
        return None
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        # KeyboardInterrupt/SystemExit 는 의도적으로 전파 — 사용자 Ctrl-C 가
        # SessionStart 90s 매달림으로 swallow 되던 회귀(audit-2026-05-24) 차단.
        _debug(f"claude -p exception: {type(e).__name__}: {e}")
        return None

    if result.returncode != 0:
        _debug(
            f"claude -p exit {result.returncode}: stderr={result.stderr[:200] if result.stderr else ''!r}"
        )
        return None
    content = (result.stdout or "").strip()
    if not content:
        _debug("claude -p empty stdout")
        return None
    return content


def build_mini_prompt(path: Path, msgs: list[dict], idx: int, total: int) -> str:
    mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime))
    recency = "가장 최근" if idx == 1 else ("가장 오래된" if idx == total else f"{idx}번째로 최근")
    lines = [
        f"다음은 Claude Code 세션 1개의 발췌입니다 ({recency}, {mtime}).",
        "이 세션 안에서만 일어난 일을 3~6줄 불릿으로 요약해주세요.",
        "",
        "포함: 작업한 프로젝트명, 핵심 결정, 해결/미해결 이슈, 다음 할 일",
        "제외: 인사말, 시스템 리마인더, 다른 세션 추측",
        "",
        "한국어. 첫 줄에 '주제:' 로 시작해서 이 세션의 중심 주제를 한 줄로 명시.",
        "날짜·버전·수치·파일경로는 발췌에 명시된 것만. 추측 금지.",
        "",
        "---발췌 시작---",
    ]
    for m in msgs:
        prefix = "U" if m["role"] == "user" else "A"
        lines.append(f"{prefix}: {m['text']}")
    lines.append("---발췌 끝---")
    return "\n".join(lines)


def build_unified_prompt(mini_summaries: list[tuple[Path, str]]) -> str:
    """legacy 2-stage용. 2026-05-22부터는 build_single_stage_prompt 직접 사용."""
    lines = [
        "아래는 최근 Claude Code 세션 여러 개의 개별 요약입니다. 가장 최근이 세션 1.",
        "이것들을 하나로 통합해, 사용자(비전공 1인 개발자)가 새 세션을 열었을 때",
        "이어서 작업할 수 있는 '지난 세션 요약'을 만들어주세요.",
        "",
        "규칙:",
        "- **가장 최근 세션(1번)의 내용을 최우선**으로 반영. 오래된 세션은 배경 맥락만.",
        "- 최근 세션에서 폐기·철회된 항목은 '미해결'에 넣지 말 것.",
        "- 프로젝트별로 묶어서 구조화.",
        "",
        "출력 형식:",
        "**진행 중인 프로젝트**",
        "**최근 결정·완료**",
        "**미해결 이슈 / 다음 할 일**",
        "",
        "한국어. 불릿 위주. 메타 설명(\"요약하면\") 금지. 날짜·버전·수치는 원문에 있는 것만.",
        "",
    ]
    for idx, (path, mini) in enumerate(mini_summaries, 1):
        mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime))
        lines.append(f"### 세션 {idx} ({mtime})")
        lines.append(mini)
        lines.append("")
    return "\n".join(lines)


def build_single_stage_prompt(session_data: list[tuple[Path, list[dict]]]) -> str:
    """1단계 통합 prompt — 5개 세션의 head/tail 메시지를 한 번에 보내 단일 요약."""
    lines = [
        "아래는 최근 Claude Code 세션 여러 개의 head/tail 메시지 발췌입니다.",
        "가장 최근이 세션 1번. 사용자(비전공 1인 개발자)가 새 세션을 열었을 때",
        "이어서 작업할 수 있는 '지난 세션 요약'을 한국어 마크다운으로 만들어주세요.",
        "",
        "규칙:",
        "- **가장 최근 세션(1번)의 내용을 최우선**. 오래된 세션은 배경 맥락만.",
        "- 최근 세션에서 폐기·철회된 항목은 '미해결'에 넣지 말 것.",
        "- 프로젝트별로 묶어서 구조화.",
        "- 메타 설명(\"요약하면\", \"여기서는\") 금지.",
        "- 날짜·버전·수치는 발췌에 명시된 것만. 추측 금지.",
        "",
        "출력 형식 (그대로 따라하기):",
        "**진행 중인 프로젝트**",
        "- ...",
        "",
        "**최근 결정·완료**",
        "- ...",
        "",
        "**미해결 이슈 / 다음 할 일**",
        "- ...",
        "",
    ]
    total = len(session_data)
    for idx, (path, msgs) in enumerate(session_data, 1):
        mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(path.stat().st_mtime))
        recency = "가장 최근" if idx == 1 else ("가장 오래된" if idx == total else f"{idx}번째")
        lines.append(f"### 세션 {idx}/{total} ({recency}, {mtime})")
        for m in msgs:
            prefix = "U" if m["role"] == "user" else "A"
            lines.append(f"{prefix}: {m['text']}")
        lines.append("")
    return "\n".join(lines)


def emit_output(summary: str) -> None:
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": f"{SIGNATURE}\n\n{summary}",
        }
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.flush()


def purge_staged_memory() -> None:
    """Sprint 3: memory/_staged/ 30일 경과 파일 청소."""
    try:
        staged = PROJECTS_DIR / "memory" / "_staged"
        if not staged.is_dir():
            return
        cutoff = time.time() - 30 * 86400
        for f in staged.glob("*.md"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                continue
    except Exception:
        pass


def trigger_background_indexer() -> None:
    """Sprint 2: 증분 인덱서를 detach 백그라운드 프로세스로 기동.
    실패해도 Sprint 1 훅 결과는 이미 출력되어 있으므로 조용히 무시."""
    try:
        import subprocess
        # v3.2.7: MV3_SCRIPTS_DIR env var 우선.
        indexer = Path(os.environ.get("MV3_SCRIPTS_DIR", "~/.claude/scripts/mindvault")).expanduser() / "indexer.py"
        if not indexer.is_file():
            return
        subprocess.Popen(
            [sys.executable, str(indexer)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        _debug(f"indexer trigger failed: {e}")


def trigger_bge_m3_warmup() -> None:
    """Sprint 8: BGE-M3 서버에 dummy embed 요청을 백그라운드로 spawn.

    launchd로 상주 중이라 모델은 메모리 상주지만, MLX forward 첫 호출 path가
    살짝 늦을 수 있음 (특히 다른 요청 처리 직후). SessionStart hook은 250ms
    제한 없는 컨텍스트라 여기서 warmup 보내두면, 직후 사용자의 첫 메시지에서
    memory-recall hook이 호출할 때 warm path 사용.
    """
    try:
        import subprocess
        code = (
            "import urllib.request, json;"
            "body = json.dumps({'input':'warmup'}).encode();"
            "req = urllib.request.Request("
            "'http://localhost:8081/embed', data=body,"
            " headers={'Content-Type':'application/json'}, method='POST');"
            "urllib.request.urlopen(req, timeout=3).read()"
        )
        subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        _debug(f"bge-m3 warmup spawn failed: {e}")


def main() -> int:
    # 무한 재귀 차단: 자기 자신의 claude -p 안에서 발동된 sub-session의 SessionStart hook은 즉시 skip
    if os.environ.get(RECURSION_GUARD_ENV) == "1":
        # stdin 비우고 silent exit (Claude Code hook 계약: exit 0)
        try:
            sys.stdin.read()
        except Exception:
            pass
        return 0
    # Sprint 8: 가능한 일찍 BGE-M3 warmup spawn. 직후 사용자 첫 메시지의
    # memory-recall hook(250ms 제한)이 warm path 활용하도록.
    trigger_bge_m3_warmup()
    try:
        # 수동 실행 편의: 환경변수로 세션 ID 지정 가능. Claude Code는 stdin JSON으로 전달.
        exclude = os.environ.get("CLAUDE_SESSION_ID")
        hook_input = sys.stdin.read() if not sys.stdin.isatty() else ""
        _debug(f"hook_input_len={len(hook_input)} tty={sys.stdin.isatty()}")
        if hook_input:
            try:
                hook_data = json.loads(hook_input)
                received_sid = (
                    hook_data.get("sessionId")
                    or hook_data.get("session_id")
                )
                _debug(f"received sessionId={received_sid!r} keys={list(hook_data.keys())}")
                exclude = received_sid or exclude
            except json.JSONDecodeError as e:
                _debug(f"hook_input json parse failed: {e}")

        _debug(f"exclude_session_id={exclude!r}")
        paths = get_recent_sessions(exclude)
        _debug(f"target sessions: {[p.name for p in paths]}")
        if not paths:
            return 0

        key = cache_key(paths)
        cached = cache_get(key)
        if cached:
            _debug(f"cache HIT key={key[:12]}")
            emit_output(cached)
            cache_purge_old()
            trigger_background_indexer()
            purge_staged_memory()
            return 0
        _debug(f"cache MISS key={key[:12]}")

        # Single-stage: 5세션 head/tail을 한 번에 prompt에 넣고 claude -p 1회 호출.
        # 2026-05-22 변경 (이전: 2-stage with 6 calls × ~11s = 66s+, 병렬화 시도해도
        # NodeJS subprocess contention으로 더 느려짐).
        session_data: list[tuple[Path, list[dict]]] = []
        for idx, p in enumerate(paths):
            head, tail = TURN_WEIGHTS[idx] if idx < len(TURN_WEIGHTS) else (4, 6)
            msgs = extract_messages(p, head_turns=head, tail_turns=tail)
            if msgs:
                session_data.append((p, msgs))

        if not session_data:
            _debug("no session data extracted")
            return 0

        unified_prompt = build_single_stage_prompt(session_data)
        summary = call_gemma(unified_prompt, max_tokens=GEMMA_UNIFIED_MAX_TOKENS)
        if not summary:
            _debug("single-stage summary failed")
            return 0

        cache_set(key, summary)
        emit_output(summary)
        cache_purge_old()
        trigger_background_indexer()
        return 0
    except Exception as e:
        _debug(f"FATAL {e}\n{traceback.format_exc()}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
