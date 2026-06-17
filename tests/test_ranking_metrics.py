"""T-B1 — ranking_metrics 순수 함수 전수 (goal B2). 임베딩/IO 0, 결정적."""
from __future__ import annotations

import math

import pytest

from ranking_metrics import (
    expected_top1_hit,
    first_relevant_hit,
    precision_at_k,
    recall_at_k,
    reciprocal_rank,
    score_corpus,
    score_query,
)


# ── recall@k ──────────────────────────────────────────────────────────
def test_recall_at_k_full_hit():
    assert recall_at_k(["a", "b", "c"], {"b"}, k=5) == 1.0


def test_recall_at_k_cutoff_miss():
    assert recall_at_k(["a", "b", "c"], {"b"}, k=1) == 0.0


def test_recall_at_k_multi_relevant_partial():
    # relevant 다중: top-2 에 b 만 → 1/2
    assert recall_at_k(["a", "b", "c"], {"b", "z"}, k=3) == 0.5


def test_recall_at_k_k_exceeds_len():
    # k > len(retrieved): 있는 것만 본다
    assert recall_at_k(["a"], {"a"}, k=5) == 1.0


def test_recall_at_k_empty_relevant():
    assert recall_at_k(["a", "b"], set(), k=5) == 0.0


def test_recall_at_k_empty_retrieved():
    assert recall_at_k([], {"a"}, k=5) == 0.0


# ── precision@k ───────────────────────────────────────────────────────
def test_precision_at_k_half():
    assert precision_at_k(["a", "b"], {"a"}, k=2) == 0.5


def test_precision_at_k_denominator_is_k():
    # k=5 인데 relevant 1건만 top-k 안 → 1/5
    assert precision_at_k(["a", "b", "c"], {"a"}, k=5) == pytest.approx(0.2)


def test_precision_at_k_zero_k():
    assert precision_at_k(["a"], {"a"}, k=0) == 0.0


def test_precision_at_k_dup_name_counts_once():
    # 중복 name 은 집합으로 1건 — 같은 메모리 중복은 1 hit
    assert precision_at_k(["a", "a"], {"a"}, k=2) == 0.5


# ── reciprocal_rank ───────────────────────────────────────────────────
def test_reciprocal_rank_third():
    assert reciprocal_rank(["x", "y", "z"], {"z"}) == pytest.approx(1 / 3)


def test_reciprocal_rank_first():
    assert reciprocal_rank(["z", "y", "x"], {"z"}) == 1.0


def test_reciprocal_rank_none():
    assert reciprocal_rank(["x", "y", "z"], {"q"}) == 0.0


def test_reciprocal_rank_empty_relevant():
    assert reciprocal_rank(["x"], set()) == 0.0


def test_reciprocal_rank_dup_uses_first_occurrence():
    assert reciprocal_rank(["a", "z", "z"], {"z"}) == 0.5


# ── first_relevant_hit ────────────────────────────────────────────────
def test_first_relevant_hit_top1_miss():
    assert first_relevant_hit(["a", "b"], {"b"}) == 0


def test_first_relevant_hit_top1_hit():
    assert first_relevant_hit(["b", "a"], {"b"}) == 1


def test_first_relevant_hit_empty_retrieved():
    assert first_relevant_hit([], {"b"}) == 0


# ── expected_top1_hit ─────────────────────────────────────────────────
def test_expected_top1_hit_match():
    assert expected_top1_hit(["b", "a"], "b") == 1


def test_expected_top1_hit_miss():
    assert expected_top1_hit(["b", "a"], "a") == 0


def test_expected_top1_hit_none_excluded():
    assert expected_top1_hit(["b", "a"], None) is None


def test_expected_top1_hit_empty_retrieved():
    assert expected_top1_hit([], "b") == 0


# ── score_query / score_corpus 집계 ───────────────────────────────────
def test_score_query_shape():
    q = score_query(["b", "a"], {"b"}, expected_top1="b", k=5)
    assert q["recall_at_k"] == 1.0
    assert q["first_relevant_hit"] == 1
    assert q["expected_top1_hit"] == 1
    assert q["reciprocal_rank"] == 1.0


def test_score_corpus_keys_present():
    pq = [score_query(["b"], {"b"}, "b", 5), score_query(["a"], {"b"}, None, 5)]
    agg = score_corpus(pq, k=5)
    for key in (
        "mean_recall_at_5",
        "mean_precision_at_5",
        "mrr",
        "first_relevant_hit_rate",
        "expected_top1_hit_rate",
        "n",
    ):
        assert key in agg, key
    assert agg["n"] == 2


def test_score_corpus_mean_matches_manual():
    # q1: recall 1.0, q2: recall 0.0 → mean 0.5
    pq = [score_query(["b", "x"], {"b"}, "b", 5), score_query(["x", "y"], {"b"}, "b", 5)]
    agg = score_corpus(pq, k=5)
    assert agg["mean_recall_at_5"] == pytest.approx(0.5)
    assert agg["first_relevant_hit_rate"] == pytest.approx(0.5)  # q1 top1 hit, q2 miss
    # expected_top1: q1 hit(b), q2 miss(x) → 0.5
    assert agg["expected_top1_hit_rate"] == pytest.approx(0.5)


def test_score_corpus_expected_top1_none_excluded_from_denominator():
    # 둘 다 hit 이지만 q2 expected_top1=None → 분모 1 → rate 1.0
    pq = [score_query(["b"], {"b"}, "b", 5), score_query(["a"], {"a"}, None, 5)]
    agg = score_corpus(pq, k=5)
    assert agg["expected_top1_hit_rate"] == 1.0


def test_score_corpus_all_none_expected_top1_is_zero():
    pq = [score_query(["b"], {"b"}, None, 5)]
    agg = score_corpus(pq, k=5)
    assert agg["expected_top1_hit_rate"] == 0.0


def test_score_corpus_empty():
    agg = score_corpus([], k=5)
    assert agg["n"] == 0
    assert agg["mean_recall_at_5"] == 0.0
    assert not math.isnan(agg["mrr"])
