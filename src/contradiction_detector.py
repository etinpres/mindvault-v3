from __future__ import annotations

import enum
import fcntl
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.memory_compiler import GEMMA_MODEL, GEMMA_TIMEOUT, GEMMA_URL


def _runtime_dir() -> Path:
    """런타임 데이터 디렉토리 (debug.log, contradictions.jsonl). env 우선."""
    env = os.environ.get("MV3_RUNTIME_DIR")
    if env:
        return Path(env)
    return Path.home() / ".claude" / "mindvault-v3"


CONFIDENCE_THRESHOLD = 0.7  # 미만은 review queue 제외 (false positive 회피)
BODY_EXCERPT_CHARS = 200    # contradictions.jsonl 에 저장할 body 발췌 길이


_CLASSIFY_PROMPT = """다음은 메모리 시스템의 두 항목입니다.
새 항목이 기존 항목과 충돌하는지 분류하세요.

[기존 항목]
{old}

[새 항목]
{new}

분류 기준:
- metric_update: 수치(%, 시간, 개수, 버전)가 갱신됨 (예: 65% → 66.3%)
- decision_reversal: 결정이 뒤집힘 (예: "X 도입" → "X 폐기")
- fact_correction: 사실이 정정됨 (예: "옛 표기 폐기" 류)
- no_conflict: 충돌 없음 (주제 다름 또는 보완 관계)

JSON만 출력. 다른 설명 없음:
{{"kind": "<one of above>", "reason": "<한 문장>", "confidence": <0.0-1.0>}}"""


def _debug(msg: str) -> None:
    """Emit to ~/.claude/mindvault-v3/debug.log (matches memory_search._debug pattern).

    MV3_RUNTIME_DIR 환경변수가 있으면 그 경로 우선, 없으면 default.
    """
    try:
        log_path = _runtime_dir() / "debug.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"[{ts}] contradiction: {msg}\n")
    except OSError:
        pass  # never raise from logging path


class ContradictionKind(str, enum.Enum):
    METRIC_UPDATE = "metric_update"
    DECISION_REVERSAL = "decision_reversal"
    FACT_CORRECTION = "fact_correction"
    NO_CONFLICT = "no_conflict"


@dataclass
class Contradiction:
    target_path: Path
    target_name: str
    kind: ContradictionKind
    reason: str
    confidence: float
    new_body_excerpt: str = ""
    old_body_excerpt: str = ""


def detect_contradictions(candidate: dict, mem_dir: Path) -> list[Contradiction]:
    """Hybrid recall + Gemma 분류로 candidate 와 mem_dir 안 충돌 후보 검출.

    Args:
        candidate: {
            "slug": str,                    # bare slug, no type prefix
            "title": str,
            "body": str,
            "type": str (optional),
            "path": Path | str (optional), # explicit self-path for exclusion
        }
        mem_dir: memory/*.md 위치 (recall 결과를 이 디렉토리 subtree 로 제한)

    Returns:
        confidence ≥ CONFIDENCE_THRESHOLD 이고 kind != NO_CONFLICT 인 항목만.
        Gemma 호출 실패 / parse 실패 / low confidence / no_conflict 는 모두 silent skip.
    """
    candidates = _recall_candidates(candidate, mem_dir, top_k=5)
    if not candidates:
        return []

    new_body = candidate.get("body", "")
    if not new_body:
        return []

    contradictions: list[Contradiction] = []
    for path, _score in candidates:
        try:
            old_body = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        result = _classify_pair(new_body, old_body)
        if not result:
            continue
        if result["kind"] == ContradictionKind.NO_CONFLICT.value:
            continue
        if result["confidence"] < CONFIDENCE_THRESHOLD:
            continue

        contradictions.append(Contradiction(
            target_path=path,
            target_name=path.stem,
            kind=ContradictionKind(result["kind"]),
            reason=result["reason"],
            confidence=result["confidence"],
            new_body_excerpt=new_body[:BODY_EXCERPT_CHARS],
            old_body_excerpt=old_body[:BODY_EXCERPT_CHARS],
        ))
    return contradictions


def _hybrid_search(query: str, mem_dir: Path, top_k: int = 5) -> list[tuple[Path, float]]:
    """memory_search.recall_memory 호출 후 (path, score) tuple 로 정규화.

    mem_dir filter: 결과 path 중 mem_dir subtree 안의 것만 (cross-project 잡음 제거).
    실패 시 빈 list + debug.log 에 사유 기록 (silent loss 방지).
    """
    from src import memory_search
    try:
        results = memory_search.recall_memory(query, top_k=top_k)
    except Exception as e:
        # Telemetry only — caller already handles []. memory_search.recall_memory
        # 자체가 FATAL+traceback 을 자기 로그에 남기므로 여기는 한 줄 요약으로 충분.
        _debug(f"recall_memory failed: {type(e).__name__}: {e}")
        return []

    mem_root = mem_dir.resolve()
    out: list[tuple[Path, float]] = []
    for r in results:
        p_raw = r.get("path")
        if not p_raw:
            continue
        try:
            p = Path(p_raw).resolve()
        except (OSError, ValueError):
            continue
        try:
            p.relative_to(mem_root)  # raises ValueError if not under mem_root
        except ValueError:
            continue
        out.append((p, float(r.get("score", 0.0))))
    return out


def _recall_candidates(
    candidate: dict, mem_dir: Path, top_k: int = 5,
) -> list[tuple[Path, float]]:
    """candidate.body+title 로 query 만들어 _hybrid_search 호출, self 제외.

    Self-exclusion 우선순위:
    1. candidate["path"] 있으면 path identity 비교 (가장 정확).
    2. 없으면 stem suffix match — production memory 파일이 "<type>_<slug>.md"
       (예: feedback_youtube_metadata_dump.md) 형태라서, slug="youtube-metadata-dump"
       만 가지고 stem 전체와 비교하면 절대 일치 안 함. suffix 매칭으로 보강.
    """
    title = candidate.get("title", "")
    body_excerpt = candidate.get("body", "")[:300]
    query = " ".join(p for p in (title, body_excerpt) if p).strip()
    if not query:
        return []

    results = _hybrid_search(query, mem_dir, top_k=top_k)

    # Self-exclusion. Prefer path identity (most reliable); fall back to stem
    # suffix match (handles "<type>_<slug>" prod naming convention).
    own_path = candidate.get("path")
    if own_path:
        try:
            own_resolved = Path(own_path).resolve()
        except (OSError, ValueError):
            own_resolved = None
    else:
        own_resolved = None

    own_slug = candidate.get("slug", "")
    own_stem_suffix = own_slug.replace("-", "_")  # underscore form

    def is_self(p: Path) -> bool:
        if own_resolved is not None and p == own_resolved:
            return True
        if not own_stem_suffix:
            return False
        # Match if stem == slug (bare) OR ends with "_<slug>" (handles type_ prefix).
        return p.stem == own_stem_suffix or p.stem.endswith("_" + own_stem_suffix)

    return [(p, s) for p, s in results if not is_self(p)]


def _call_gemma_for_classify(prompt: str, max_tokens: int = 400) -> str | None:
    """Gemma 4 E4B 호출. 실패 시 None (silent, _debug 로깅).

    BaseException 은 통과시킴 (sentinel pattern, hook hard-budget 호환).
    """
    body = json.dumps({
        "model": GEMMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.0,
    }).encode()
    req = urllib.request.Request(
        GEMMA_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=GEMMA_TIMEOUT) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError, OSError, TimeoutError) as e:
        _debug(f"gemma classify fail: {type(e).__name__}: {e}")
        return None

    choices = data.get("choices") or []
    if not choices:
        return None
    content = (choices[0].get("message") or {}).get("content") or ""
    return content.strip() or None


def _strip_code_fences(text: str) -> str:
    """```json\\n{...}\\n``` 같은 마크다운 fence 제거."""
    text = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```\s*$", text, re.DOTALL)
    return m.group(1) if m else text


def _classify_pair(new_body: str, old_body: str) -> dict | None:
    """두 body 비교 후 {'kind', 'reason', 'confidence'} 반환. failure → None.

    body 는 1500자까지만 prompt 에 포함 (Gemma 4K context 여유).
    """
    prompt = _CLASSIFY_PROMPT.format(old=old_body[:1500], new=new_body[:1500])
    raw = _call_gemma_for_classify(prompt)
    if not raw:
        return None

    raw = _strip_code_fences(raw)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None

    if not isinstance(parsed, dict):
        return None

    valid_kinds = {k.value for k in ContradictionKind}
    if parsed.get("kind") not in valid_kinds:
        return None

    parsed.setdefault("confidence", 0.5)
    parsed.setdefault("reason", "")

    try:
        parsed["confidence"] = float(parsed["confidence"])
    except (TypeError, ValueError):
        parsed["confidence"] = 0.5

    return parsed


def append_to_review_queue(
    candidate_slug: str,
    contradictions: list[Contradiction],
    new_path: Path,
) -> Path:
    """Contradiction 항목들을 contradictions.jsonl 에 append.

    동시 write race 회피: fcntl.flock(LOCK_EX) for the batch (parallel SessionEnd
    hooks 가 sibling Conductor workspaces 에서 동시 호출될 수 있음).
    OSError silent skip + _debug log (T5 hook context 에서 traceback 노이즈 회피).
    Timestamp 는 UTC (self_eval.py:135 naive TZ 경고 따름).
    """
    out = _runtime_dir() / "contradictions.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        with out.open("a", encoding="utf-8") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            except OSError as e:
                _debug(f"queue flock fail: {type(e).__name__}: {e}")
                # Continue without lock — better single-writer integrity than full skip

            for c in contradictions:
                f.write(json.dumps({
                    "ts": ts,
                    "new_slug": candidate_slug,
                    "new_path": str(new_path),
                    "target_name": c.target_name,
                    "target_path": str(c.target_path),
                    "kind": c.kind.value,
                    "reason": c.reason,
                    "confidence": c.confidence,
                    "new_excerpt": c.new_body_excerpt,
                    "old_excerpt": c.old_body_excerpt,
                    "resolved": False,
                }, ensure_ascii=False) + "\n")
            # flock auto-released on file close
    except OSError as e:
        _debug(f"queue append fail: {type(e).__name__}: {e}")

    return out
