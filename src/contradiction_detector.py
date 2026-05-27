from __future__ import annotations

import enum
from dataclasses import dataclass
from pathlib import Path


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
        candidate: {"slug", "title", "body", "type" (optional)}
        mem_dir: memory/*.md 위치

    Returns:
        confidence ≥ CONFIDENCE_THRESHOLD 이고 kind != NO_CONFLICT 만.
    """
    return []  # 후속 tasks (T2~T4) 에서 채움
