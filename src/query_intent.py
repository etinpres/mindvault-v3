#!/usr/bin/env python3
"""MindVault v3 Sprint 16 — Query Intent Classifier (rule-based).

memory-recall.py 의 hook 게이트 보강. raw_cosine 단일 게이트는 잡담·메타가
우연 단어 매칭으로 mid-cosine zone (0.30~0.40) 진입할 때 차단 불완전.
intent 가 chat/meta 면 회수 0건 강제 — V3-PLAN §3.D 의 mid-cosine zone discriminator.

규칙 기반 우선 (Gemma 미사용):
- hook latency 추가 0 (regex 매칭은 마이크로초)
- 잡담·메타 한국어 패턴은 규칙적 → 휴리스틱 충분
- Gemma 호출은 옵션으로 별도 추가 가능 (현재 sprint 미포함)

분류 결과:
- chat: 인사·짧은 잡담
- meta: Claude/세션 메타 대화
- code: 코드·파일·실행 요청
- recall: 명시적 회수 의도 ("예전에", "기억나")
- unknown: 위 어디에도 안 잡힘 — hook 기본 게이트 그대로
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from pathlib import Path
from typing import NamedTuple

# Sprint NEXT-3 — Gemma 보강 classifier. rule-based 가 unknown 으로 떨어진
# 짧은 query 에 한해 Gemma 가 chat/meta 분류 보강. opt-in 환경변수로 default off.
GEMMA_INTENT_URL = "http://localhost:8080/v1/chat/completions"
GEMMA_INTENT_MODEL = "mlx-community/gemma-4-e4b-it-4bit"
GEMMA_INTENT_TIMEOUT = 2.0
GEMMA_INTENT_MAX_LEN = 40  # 그 이상 query 는 Gemma 호출 안 함 (cost / latency)
ENABLE_GEMMA_INTENT_ENV = "MV2_GEMMA_INTENT"

_DEBUG_LOG = Path("/Users/yonghaekim/.claude/mindvault-v2/debug.log")


def _debug(msg: str) -> None:
    try:
        _DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _DEBUG_LOG.open("a") as f:
            f.write(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] intent: {msg}\n"
            )
    except Exception:
        pass

# 각 카테고리 regex. compile 1회.
# 우선순위: recall > code > meta > chat > unknown.
# 같은 query 가 여러 카테고리 매칭하면 우선순위 높은 쪽 채택.

CHAT_RE = re.compile(
    r"(^(?:안녕|안녕하세요|하이|반갑|굿모닝|굿나잇|좋은\s?(?:아침|밤|저녁|하루))|"
    r"^(?:오늘\s?(?:날씨|기분|점심|저녁|뭐|어때)|날씨\s?어때|기분\s?어때)|"
    r"^(?:고마워|감사합니다|땡큐|땡스)|"
    r"^(?:잘자|굿나잇|그럼\s?이만|나중에|또\s?봐))"
)

META_RE = re.compile(
    r"(무슨\s?모델|어떤\s?모델|어떤\s?(?:버전|claude)|claude\s?(?:몇|version)|"
    r"context\s?(?:얼마|남았|window|용량)|토큰\s?(?:얼마|남았|사용)|"
    r"너는\s?(?:누구|뭐|어떤)|당신은\s?(?:누구|뭐)|네\s?이름|"
    r"claude\s?code|이\s?세션|현재\s?세션|버전\s?(?:이|확인)|모델\s?(?:이|확인))"
)

# code intent — 명확한 코드 작업 키워드 또는 파일 경로/확장자
_FILE_EXT_RE = re.compile(
    r"\.(?:py|js|jsx|ts|tsx|md|yml|yaml|toml|json|sh|bash|zsh|c|cc|cpp|h|hpp|rs|go|java|kt|swift|rb|php|sql|html|css|scss)\b",
    re.IGNORECASE,
)
_PATH_HINT_RE = re.compile(r"(?:^|\s)([/\\]?[\w.-]+/[\w./-]+)")
CODE_RE = re.compile(
    r"(이\s?(?:함수|코드|클래스|메서드|메소드|파일|버그|테스트|커밋|브랜치)|"
    r"버그\s?(?:고쳐|수정|fix)|fix\s?(?:bug|this)|"
    r"(?:테스트|test)\s?(?:돌려|실행|run)|돌려\s?봐|run\s?(?:the\s)?test|"
    r"(?:배포|deploy|ship)|commit|커밋|push|머지|merge|pr\s?(?:만들|올려|생성)|"
    r"리팩토링|refactor|reindex|컴파일|build\s?(?:해|돌려)|타입\s?체크|"
    r"실행\s?(?:해|돌려)|실행해\s?봐)",
    re.IGNORECASE,  # PR/Test/Commit 등 대문자 case 포용
)

RECALL_RE = re.compile(
    r"(예전에|그때|이전에|지난번|어제|전에|옛날에|저번에|"
    r"기억(?:해|나|안\s?나|에)|뭐였(?:어|지|더|을까)|"
    r"이전\s?(?:대화|세션)|예전\s?(?:대화|일|얘기))"
)

MIN_LEN_CHAT_FALLBACK = 6
# 짧은 인삿말 외 길이도 너무 짧고 단어 1~2개면 chat fallback


class IntentResult(NamedTuple):
    intent: str
    confidence: float
    matched: list[str]


def _matched_terms(regex: re.Pattern, text: str) -> list[str]:
    return [m.group(0) for m in regex.finditer(text)]


def classify(prompt: str) -> IntentResult:
    """rule-based intent. 우선순위: recall > code > meta > chat > unknown."""
    if not prompt:
        return IntentResult("unknown", 0.0, [])
    p = prompt.strip()

    # recall: 가장 강력한 신호 (raw cosine 게이트 완화 의도)
    recall_hits = _matched_terms(RECALL_RE, p)
    if recall_hits:
        return IntentResult("recall", min(1.0, 0.6 + 0.1 * len(recall_hits)), recall_hits)

    # code: file/extension 또는 코드 키워드
    code_hits = _matched_terms(CODE_RE, p)
    ext_hits = _matched_terms(_FILE_EXT_RE, p)
    if code_hits or ext_hits:
        merged = code_hits + ext_hits
        return IntentResult(
            "code", min(1.0, 0.6 + 0.1 * len(merged)), merged[:5]
        )

    # meta: Claude/세션 메타
    meta_hits = _matched_terms(META_RE, p)
    if meta_hits:
        return IntentResult(
            "meta", min(1.0, 0.7 + 0.1 * len(meta_hits)), meta_hits
        )

    # chat: 인사·잡담 + 짧은 query 휴리스틱
    chat_hits = _matched_terms(CHAT_RE, p)
    if chat_hits:
        return IntentResult(
            "chat", min(1.0, 0.7 + 0.1 * len(chat_hits)), chat_hits
        )
    # 짧고 단어 적으면 chat fallback — 의도가 약함
    word_count = len(re.findall(r"[가-힣A-Za-z0-9]+", p))
    if len(p) < MIN_LEN_CHAT_FALLBACK and word_count <= 2:
        return IntentResult("chat", 0.4, ["short-fallback"])

    return IntentResult("unknown", 0.0, [])


def should_skip_recall(intent: IntentResult) -> bool:
    """intent 가 chat/meta 면 회수 skip. 게이트 통과해도 차단."""
    return intent.intent in ("chat", "meta")


_GEMMA_INTENT_PROMPT = (
    "다음 질문을 한 단어로 분류해라. 분류 라벨만 한 줄 출력 (해설 금지):\n"
    "chat: 인사·잡담·날씨·기분\n"
    "meta: Claude·세션·모델·버전·토큰 등 자기참조\n"
    "code: 코드·파일·실행·디버그·테스트·빌드\n"
    "recall: 과거 대화·결정·메모리 회수\n"
    "other: 그 외 작업 지시\n\n"
    "질문: {q}\n"
    "분류:"
)

_VALID_GEMMA_LABELS = {"chat", "meta", "code", "recall", "other"}


def gemma_intent_enabled() -> bool:
    """env-based opt-in. session_memory_end / hook 이 사용."""
    return os.environ.get(ENABLE_GEMMA_INTENT_ENV, "").strip() == "1"


def _call_gemma_intent(prompt_text: str) -> str | None:
    """Gemma 한 줄 분류 호출. 실패하면 None."""
    body = json.dumps(
        {
            "model": GEMMA_INTENT_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": _GEMMA_INTENT_PROMPT.format(q=prompt_text),
                }
            ],
            "max_tokens": 8,
            "temperature": 0.0,
        }
    ).encode()
    req = urllib.request.Request(
        GEMMA_INTENT_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=GEMMA_INTENT_TIMEOUT) as resp:
            data = json.loads(resp.read())
        choices = data.get("choices") or []
        if not choices:
            return None
        content = (choices[0].get("message") or {}).get("content") or ""
        return content.strip() or None
    except Exception as e:
        _debug(f"gemma intent fail: {type(e).__name__} {e}")
        return None


_LABEL_TOKEN_RE = re.compile(r"[a-zA-Z]+")


def _normalize_gemma_label(raw: str | None) -> str | None:
    """Gemma 응답의 첫 영문 토큰을 lowercase 라벨로. 유효 라벨만 반환."""
    if not raw:
        return None
    m = _LABEL_TOKEN_RE.search(raw)
    if not m:
        return None
    label = m.group(0).lower()
    return label if label in _VALID_GEMMA_LABELS else None


def classify_with_gemma(prompt: str) -> IntentResult | None:
    """rule-based 가 unknown 으로 떨어진 짧은 query 를 Gemma 가 보강 분류.

    호출 조건은 caller (hook) 가 판단 — 본 함수는 opt-in 체크만 추가로 안 함.
    유효 라벨(`chat`/`meta`/`code`/`recall`/`other`) 만 반환. 그 외엔 None.
    실패(timeout, 서버 다운, parse fail)는 None 반환 — rule-based 결과로 폴백.
    """
    p = (prompt or "").strip()
    if not p or len(p) > GEMMA_INTENT_MAX_LEN:
        return None
    raw = _call_gemma_intent(p)
    label = _normalize_gemma_label(raw)
    if label is None:
        return None
    if label == "other":
        # other 는 unknown 과 동의 — 보강 효과 없음. 본 hook 흐름은
        # rule-based unknown 그대로 사용하는 게 더 안전.
        return None
    _debug(f"gemma label={label} q={p[:40]!r}")
    return IntentResult(label, 0.6, [f"gemma:{label}"])
