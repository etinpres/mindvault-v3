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
VALID_TYPES = ("feedback", "project")

TRIGGER_RE = re.compile(
    r"(기억해|잊지\s?마|잊지말아|결정[:：]|정했[어다]|앞으로는|다음부턴|"
    r"이 프로젝트는|원칙[:：]|규칙[:：])"
)

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
                text = extract_text_from_content(msg.get("content")).strip()
                if not text:
                    continue
                if text.startswith("# 지난 세션 요약"):
                    continue
                msgs.append({"role": t, "text": redact(text)})
    except OSError as e:
        _debug(f"read fail {jsonl_path.name}: {e}")
        return []
    return msgs[-tail_turns:]


def has_trigger(messages: list[dict]) -> bool:
    for m in messages:
        if m["role"] != "user":
            continue
        if TRIGGER_RE.search(m["text"]):
            return True
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
        prefix = "U" if m["role"] == "user" else "A"
        excerpt_parts.append(f"{prefix}: {m['text'][:600]}")
    excerpt = "\n".join(excerpt_parts)
    return (
        "아래는 Claude Code 세션 대화 마지막 부분이다. 사용자가 '영구 기억'으로 남기려고 한 "
        "사실만 추출하라. 주관적 의견·일회성 대화·진행 보고는 제외.\n\n"
        "출력은 JSON 배열만. 각 항목 형식:\n"
        '{"type":"feedback|project","title":"한 줄 50자 이내","body":"본문 200자 이내",'
        '"reason":"저장 이유 10자 이내","evidence":"원문 인용 30자"}\n\n'
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
