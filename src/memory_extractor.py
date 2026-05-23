#!/usr/bin/env python3
"""MindVault v2 Sprint 3 — Gemma 기반 기억 후보 추출기.

트리거 키워드 감지 → Gemma에 구조화 프롬프트 → JSON 배열 파싱 → 유효 항목 반환.
"""
from __future__ import annotations

import json
import re
import time
import traceback
import urllib.request
from pathlib import Path

DATA_DIR = Path("/Users/yonghaekim/.claude/mindvault-v2")
DEBUG_LOG = DATA_DIR / "debug.log"
GEMMA_URL = "http://localhost:8080/v1/chat/completions"
GEMMA_MODEL = "mlx-community/gemma-4-e4b-it-4bit"
GEMMA_TIMEOUT = 45
MAX_BODY_CHARS = 200
MAX_TITLE_CHARS = 50
# Sprint 13: procedural type 추가 — 명령어 syntax·workflow·환경 설정.
# 새 trigger 그룹으로 신호 받아 Gemma 가 type=procedural 후보 생성하면
# session_memory_end 가 _procedural/_staged/ 슬롯에 저장.
VALID_TYPES = ("feedback", "project", "procedural")

TRIGGER_RE = re.compile(
    r"(기억해|잊지\s?마|잊지말아|결정[:：]|정했[어다]|앞으로는|다음부턴|"
    r"이 프로젝트는|원칙[:：]|규칙[:：]|"
    # Sprint 13 procedural triggers — 명령어·문법·workflow·환경설정 자동 추출
    r"이 명령어|이 명령은|이렇게\s?(?:쓰면|하면|입력하면|실행하면)|"
    r"syntax|문법|이\s?(?:패턴|workflow|플로우|순서|절차)는?|"
    r"외워둬|기억해둬|반복(?:해서|적으로)\s?(?:쓰|할|사용|실행)|"
    r"환경설정|환경\s?변수|셋업|setup|이\s?(?:flag|옵션|플래그))"
)

# Sprint NEXT-1 자동 trigger 휴리스틱 — assistant 의 Bash tool_use 안 명령어가
# 특수 binary 또는 non-trivial 한 모양인데, 직후 user 가 다음 액션을 지시하면
# (한국어 사용자 패턴상 "ok/굿" 같은 confirmation 보다 "진행/적용/켜줘" 형이 흔하다)
# Gemma 가 보고 procedural candidate 만들 가치 있다고 본다. trigger ON.
SPECIAL_BIN_RE = re.compile(
    r"\b(?:launchctl|sqlite3|ffprobe|ffmpeg|yt-dlp|higgsfield|kubectl|gcloud|"
    r"hyperframes|jq|awk)\b|sed\s+-i\b|gh\s+api\b|"
    r"claude\s+(?:--bg|-c\b|--resume|-r\b)|"
    r"git\s+worktree\b"
)
NEXT_ACTION_RE = re.compile(
    r"(진행|해결|적용|켜줘|실행|영구화|반영|배포|sync|push|land|merge|commit|"
    r"ship|다음|이어서|계속)"
)


def _is_non_trivial_bash(cmd: str) -> bool:
    """길이 100 이상, 또는 pipe/redirect 2+ 회 — '복잡한 한 줄'."""
    if not cmd:
        return False
    if len(cmd) >= 100:
        return True
    if cmd.count(" | ") >= 2:
        return True
    if cmd.count(">") + cmd.count(">>") >= 2:
        return True
    return False


def _is_special_bash(cmd: str) -> bool:
    if not cmd:
        return False
    return bool(SPECIAL_BIN_RE.search(cmd))

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
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] extractor: {msg}\n")
    except Exception:
        pass


def redact(text: str) -> str:
    for pat, repl in SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return text


def _is_system_reminder(text: str) -> bool:
    head = text.lstrip()[:50]
    return head.startswith("<system-reminder>") or head.startswith("<command-")


def extract_text_from_content(content) -> str:
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


def extract_bash_from_content(content) -> list[str]:
    """assistant message 안의 Bash tool_use command 문자열만 수집."""
    if not isinstance(content, list):
        return []
    cmds: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") != "tool_use" or block.get("name") != "Bash":
            continue
        inp = block.get("input") or {}
        cmd = inp.get("command") if isinstance(inp, dict) else None
        if isinstance(cmd, str) and cmd.strip():
            cmds.append(redact(cmd.strip()))
    return cmds


def load_tail_messages(jsonl_path: Path, tail_turns: int = 40) -> list[dict]:
    msgs: list[dict] = []
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
                bash_cmds = (
                    extract_bash_from_content(content) if t == "assistant" else []
                )
                if not text and not bash_cmds:
                    continue
                if text.startswith("# 지난 세션 요약"):
                    continue
                msgs.append(
                    {
                        "role": t,
                        "text": redact(text),
                        "bash_commands": bash_cmds,
                    }
                )
    except OSError as e:
        _debug(f"read fail {jsonl_path.name}: {e}")
        return []
    return msgs[-tail_turns:]


def has_trigger(messages: list[dict]) -> bool:
    """기존 키워드 trigger OR Sprint NEXT-1 자동 휴리스틱.

    휴리스틱: 직전 assistant 의 Bash tool_use 가 special_binary 또는
    non_trivial 인데, 직후 user 가 NEXT_ACTION 표현으로 응답하면
    procedural 후보 가치가 있다고 본다 (Gemma 가 최종 판별).
    """
    prev_bash_signal = False
    for m in messages:
        role = m.get("role")
        text = m.get("text", "") or ""
        if role == "assistant":
            cmds = m.get("bash_commands") or []
            # 한 user turn 사이의 assistant 분할(tool_use → text) 을 흡수 — 한 번이라도
            # special/non_trivial 가 보였으면 signal 누적
            if any(_is_special_bash(c) or _is_non_trivial_bash(c) for c in cmds):
                prev_bash_signal = True
            continue
        if role != "user":
            continue
        if TRIGGER_RE.search(text):
            return True
        if (
            prev_bash_signal
            and len(text) <= 50
            and NEXT_ACTION_RE.search(text)
        ):
            return True
        # user turn 마침 → 다음 사이클 위해 reset
        prev_bash_signal = False
    return False


def call_gemma(prompt: str, max_tokens: int = 1500) -> str | None:
    body = json.dumps(
        {
            "model": GEMMA_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": 0.2,
        }
    ).encode()
    req = urllib.request.Request(
        GEMMA_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=GEMMA_TIMEOUT) as resp:
            data = json.loads(resp.read())
        choices = data.get("choices") or []
        if not choices:
            return None
        content = (choices[0].get("message") or {}).get("content") or ""
        return content.strip() or None
    except Exception as e:
        _debug(f"gemma fail: {type(e).__name__} {e}")
        return None


def build_prompt(messages: list[dict]) -> str:
    excerpt_parts = []
    for m in messages:
        role = m.get("role")
        prefix = "U" if role == "user" else "A"
        text = m.get("text") or ""
        if text:
            excerpt_parts.append(f"{prefix}: {text[:600]}")
        # assistant 의 Bash command 도 Gemma 가 보도록 별도 라인으로 첨부
        for cmd in (m.get("bash_commands") or [])[:5]:
            excerpt_parts.append(f"{prefix}:bash: {cmd[:300]}")
    excerpt = "\n".join(excerpt_parts)
    return (
        "아래는 Claude Code 세션 대화 마지막 부분이다. 사용자가 '영구 기억'으로 남기려고 한 "
        "사실만 추출하라. 주관적 의견·일회성 대화·진행 보고는 제외.\n\n"
        "출력은 JSON 배열만. 각 항목 형식:\n"
        '{"type":"feedback|project|procedural","title":"한 줄 50자 이내","body":"본문 200자 이내",'
        '"reason":"저장 이유 10자 이내","evidence":"원문 인용 30자"}\n\n'
        "type 가이드:\n"
        "- feedback: 사용자의 작업 방식·선호·금지사항 (예: '커밋 분리해라', '머지 직접 금지')\n"
        "- project: 프로젝트 상태·결정·인물·외부 자원 (예: 'X 출시 2026-05', '책임자 Y')\n"
        "- procedural: 명령어·syntax·workflow·환경 설정. body 는 실행 예시 1줄 + 한 줄 설명.\n"
        "  예: body='claude --bg \"prompt\" # 백그라운드 세션 시작. 결과는 jobs 디렉토리에 저장.'\n\n"
        "후보가 없으면 빈 배열 []. 해설·마크다운 코드펜스 금지. JSON만.\n\n"
        "---대화---\n"
        f"{excerpt}\n"
        "---끝---"
    )


def parse_gemma_json(out: str) -> list[dict]:
    if not out:
        return []
    m = re.search(r"\[[\s\S]*\]", out)
    if not m:
        return []
    try:
        arr = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        _debug(f"json parse fail: {e}")
        return []
    if not isinstance(arr, list):
        return []
    valid = []
    for item in arr:
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        title = (item.get("title") or "").strip()
        body = (item.get("body") or "").strip()
        if t not in VALID_TYPES or not title or not body:
            continue
        valid.append(
            {
                "type": t,
                "title": title[:MAX_TITLE_CHARS],
                "body": body[:MAX_BODY_CHARS],
                "reason": (item.get("reason") or "").strip()[:30],
                "evidence": (item.get("evidence") or "").strip()[:60],
            }
        )
    return valid


def extract_from_jsonl(jsonl_path: Path) -> list[dict]:
    try:
        msgs = load_tail_messages(jsonl_path)
        if not msgs:
            return []
        if not has_trigger(msgs):
            _debug(f"no trigger in {jsonl_path.name}, skip")
            return []
        prompt = build_prompt(msgs)
        out = call_gemma(prompt)
        if not out:
            return []
        return parse_gemma_json(out)
    except Exception as e:
        _debug(f"extract FATAL: {e}\n{traceback.format_exc()}")
        return []
