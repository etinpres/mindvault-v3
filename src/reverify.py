#!/usr/bin/env python3
"""MindVault v3 Phase 1 ③ — 신뢰성 검증 (stale 자동 감지, over-trust 해소).

메모리의 코드/사실 참조(모델명·포트)를 현행 코드와 결정론적으로 대조해 stale
의심을 판정한다. Layer 5 모순감지(memory vs memory)와 달리 ③은 memory vs 현행
코드. Gemma 미사용 — 운영비 0, 결정론, CI pin 가능.

판별 신호(설계 §2): 메모리가 stale_alias 토큰을 포함하면서 current_value 토큰을
미포함하면 stale 의심. 현행 값을 함께 언급하는 이력 메모리는 면제. verifier 가
current_value 가 라이브 코드에 실재하는지 확인 → registry 자체의 메타-staleness 차단.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional


def default_root() -> Path:
    """현행 코드 ground truth root. MV3_REVERIFY_ROOT env 우선, 기본 = repo root."""
    env = os.environ.get("MV3_REVERIFY_ROOT", "").strip()
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parent.parent  # src/reverify.py → repo root


def _grep_present(root: Path, rel_path: str, pattern: str) -> bool:
    """root/rel_path 에 pattern(정규식, 대소문자 무시)이 존재하면 True (없으면 False)."""
    try:
        text = (root / rel_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return bool(re.search(pattern, text, re.IGNORECASE))


@dataclass(frozen=True)
class CanonicalFact:
    key: str
    current_value: str            # 현행 진실 토큰 (회수 메모리가 이걸 언급하면 면제)
    stale_aliases: tuple          # 현재처럼 주장되면 stale 인 옛 토큰들
    verifier: Callable            # (root: Path) -> bool : current_value 가 라이브?
    description: str = ""


# 초기 facts — 실측 stale 위험 + verifier 라이브 통과 확인 (설계 D3).
# 확장: 형이 summarizer 포트·버전·파일경로 등 한 줄씩 추가 (단 verifier 라이브 통과 필수).
CANONICAL_FACTS = (
    CanonicalFact(
        key="embedding_model",
        current_value="arctic",
        stale_aliases=("bge-m3", "bge_m3"),
        verifier=lambda root: _grep_present(root, "src/memory_indexer.py", r"arctic"),
        description="임베딩 모델 (Sprint 9/14 BGE-M3 → Arctic-ko 교체)",
    ),
    CanonicalFact(
        key="embedding_port",
        current_value="8081",
        stale_aliases=("8765",),
        verifier=lambda root: _grep_present(root, "src/memory_indexer.py", r"(?<!\d)8081(?!\d)"),
        description="임베딩 서버 포트 (Arctic-ko :8081)",
    ),
)


def _contains_token(text: str, token: str) -> bool:
    """대소문자 무시 토큰 포함 검사. 숫자 토큰은 word-boundary(18081 안 8081 오매칭 차단)."""
    t = text.lower()
    tok = token.lower()
    if tok.isdigit():
        return re.search(rf"(?<!\d){re.escape(tok)}(?!\d)", t) is not None
    return tok in t


@dataclass
class StaleVerdict:
    status: str                   # "stale" | "fresh"
    note: str = ""
    findings: List[str] = field(default_factory=list)


def check_memory_staleness(
    text: str, root: Optional[Path] = None, facts=CANONICAL_FACTS
) -> StaleVerdict:
    """메모리 텍스트(frontmatter+body)를 현행 코드와 대조해 stale 판정 (설계 §2).

    각 fact 에 대해: verifier(root) 가 current_value 라이브 확인 못 하면 skip
    (registry stale 의심 → verify_registry). current_value 토큰 동반이면 면제(이력).
    stale_alias 토큰만 있으면 → finding 누적. finding 있으면 stale.
    """
    if root is None:
        root = default_root()
    findings: List[str] = []
    for fact in facts:
        if not fact.verifier(root):
            continue  # current_value 라이브 확인 불가 → 이 fact 로 판정 안 함
        if _contains_token(text, fact.current_value):
            continue  # 현행 값 동반 → 정당한 이력/현행, 면제
        hit = next((a for a in fact.stale_aliases if _contains_token(text, a)), None)
        if hit:
            findings.append(
                f"{fact.key}: '{hit}' 현재형 참조, 현행 {fact.current_value} 미언급"
            )
    if findings:
        return StaleVerdict(status="stale", note="; ".join(findings), findings=findings)
    return StaleVerdict(status="fresh")


def verify_registry(root: Optional[Path] = None, facts=CANONICAL_FACTS) -> List[dict]:
    """각 fact 의 current_value 가 라이브 코드에 실재하는지 self-check.

    반환: verifier fail 한 fact 들 [{key, description}] — registry stale 경고용.
    빈 리스트 = 레지스트리 정상.
    """
    if root is None:
        root = default_root()
    return [
        {"key": f.key, "description": f.description}
        for f in facts
        if not f.verifier(root)
    ]
