#!/usr/bin/env python3
"""MindVault v3 — 회수 랭킹 메트릭 (gbrain `correctness-gate.ts`·`qrels-file.ts` 이식).

순수 함수만. 임베딩/IO 0, 결정적(B5). `retrieved`는 회수 결과의 메모리 `name`
순위 리스트(`recall_memory` 반환 dict의 `name` 필드에서 추출), `relevant`는 정답
`name` 집합.

규약:
- recall@k = |set(retrieved[:k]) ∩ relevant| / |relevant|   (plan §1.2, 집합 기반)
- precision@k = |set(retrieved[:k]) ∩ relevant| / k         (표준 IR, 분모는 컷오프 k)
- reciprocal_rank = 1/rank (첫 relevant 의 1-indexed 순위), 없으면 0.0
- first_relevant_hit = retrieved[0] ∈ relevant 면 1 else 0   (top-1 정확도)
- expected_top1_hit = retrieved[0] == expected_top1 면 1 else 0; expected_top1 None 이면 None
                       (None 은 expected_top1_hit_rate 분모에서 제외)
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence


def _topk_set(retrieved: Sequence[str], k: int) -> set[str]:
    """retrieved[:k] 의 집합 (중복 name 은 1건으로 축약 — 같은 메모리 중복은 1 hit)."""
    if k < 0:
        k = 0
    return set(retrieved[:k])


def precision_at_k(retrieved: Sequence[str], relevant: Iterable[str], k: int) -> float:
    """top-k 안의 relevant 메모리 수 / k. k<=0 이면 0.0."""
    if k <= 0:
        return 0.0
    rel = set(relevant)
    if not rel:
        return 0.0
    hits = len(_topk_set(retrieved, k) & rel)
    return hits / k


def recall_at_k(retrieved: Sequence[str], relevant: Iterable[str], k: int) -> float:
    """|top-k ∩ relevant| / |relevant|. relevant 비면 0.0(분모 0 회피)."""
    rel = set(relevant)
    if not rel:
        return 0.0
    hits = len(_topk_set(retrieved, k) & rel)
    return hits / len(rel)


def first_relevant_hit(retrieved: Sequence[str], relevant: Iterable[str]) -> int:
    """retrieved[0] 이 relevant 면 1 else 0. 빈 retrieved → 0."""
    rel = set(relevant)
    if not retrieved or not rel:
        return 0
    return 1 if retrieved[0] in rel else 0


def expected_top1_hit(retrieved: Sequence[str], expected_top1: str | None) -> int | None:
    """retrieved[0] == expected_top1 면 1 else 0. expected_top1 None → None(분모 제외)."""
    if expected_top1 is None:
        return None
    if not retrieved:
        return 0
    return 1 if retrieved[0] == expected_top1 else 0


def reciprocal_rank(retrieved: Sequence[str], relevant: Iterable[str]) -> float:
    """첫 relevant 의 1/rank(1-indexed). 없으면 0.0. 중복 name 은 첫 등장 기준."""
    rel = set(relevant)
    if not rel:
        return 0.0
    for idx, name in enumerate(retrieved):
        if name in rel:
            return 1.0 / (idx + 1)
    return 0.0


def score_query(
    retrieved: Sequence[str],
    relevant: Iterable[str],
    expected_top1: str | None,
    k: int,
) -> dict:
    """한 쿼리의 메트릭 묶음 — 러너가 per-query 로 호출, score_corpus 가 집계."""
    rel = set(relevant)
    return {
        "recall_at_k": recall_at_k(retrieved, rel, k),
        "precision_at_k": precision_at_k(retrieved, rel, k),
        "reciprocal_rank": reciprocal_rank(retrieved, rel),
        "first_relevant_hit": first_relevant_hit(retrieved, rel),
        "expected_top1_hit": expected_top1_hit(retrieved, expected_top1),
    }


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def score_corpus(per_query: list[dict], k: int = 5) -> dict:
    """per-query 메트릭 dict 리스트 → 코퍼스 집계.

    각 per_query dict 은 score_query 가 낸 키(recall_at_k/precision_at_k/
    reciprocal_rank/first_relevant_hit/expected_top1_hit)를 가진다고 가정.
    expected_top1_hit_rate 는 None 이 아닌 쿼리만 분모(전부 None 이면 0.0).
    출력 키: mean_recall_at_{k}, mean_precision_at_{k}, mrr,
             first_relevant_hit_rate, expected_top1_hit_rate, n
    """
    n = len(per_query)
    recalls = [float(q["recall_at_k"]) for q in per_query]
    precisions = [float(q["precision_at_k"]) for q in per_query]
    rrs = [float(q["reciprocal_rank"]) for q in per_query]
    first_hits = [float(q["first_relevant_hit"]) for q in per_query]
    et1_vals = [
        float(q["expected_top1_hit"])
        for q in per_query
        if q.get("expected_top1_hit") is not None
    ]
    return {
        f"mean_recall_at_{k}": _mean(recalls),
        f"mean_precision_at_{k}": _mean(precisions),
        "mrr": _mean(rrs),
        "first_relevant_hit_rate": _mean(first_hits),
        "expected_top1_hit_rate": _mean(et1_vals),
        "n": n,
    }
