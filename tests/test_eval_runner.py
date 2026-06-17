"""T-B3 / T-B1b / T-B4 — eval_runner (qrels 스키마·러너 리포트·결정성)."""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from eval_runner import load_qrels, run_corpus

REPO = Path(__file__).resolve().parent.parent
REAL_QRELS = REPO / "evals" / "recall_qrels.json"


# ── T-B3 — qrels 스키마 검증 ──────────────────────────────────────────
def _write_qrels(tmp_path, obj) -> Path:
    p = tmp_path / "q.json"
    p.write_text(json.dumps(obj), encoding="utf-8")
    return p


def test_load_real_qrels_ok():
    data = load_qrels(REAL_QRELS)
    assert data["schema_version"] == 1
    assert len(data["queries"]) >= 20  # goal B1: ≥20


def test_load_qrels_bad_schema_version(tmp_path):
    p = _write_qrels(tmp_path, {"schema_version": 2, "queries": [{"query_id": "a", "query": "x", "relevant": ["m"]}]})
    with pytest.raises(ValueError, match="schema_version"):
        load_qrels(p)


def test_load_qrels_empty_queries(tmp_path):
    p = _write_qrels(tmp_path, {"schema_version": 1, "queries": []})
    with pytest.raises(ValueError, match="non-empty"):
        load_qrels(p)


def test_load_qrels_empty_relevant(tmp_path):
    p = _write_qrels(tmp_path, {"schema_version": 1, "queries": [{"query_id": "a", "query": "x", "relevant": []}]})
    with pytest.raises(ValueError, match="relevant"):
        load_qrels(p)


def test_load_qrels_missing_relevant(tmp_path):
    p = _write_qrels(tmp_path, {"schema_version": 1, "queries": [{"query_id": "a", "query": "x"}]})
    with pytest.raises(ValueError, match="relevant"):
        load_qrels(p)


def test_load_qrels_duplicate_query_id(tmp_path):
    p = _write_qrels(tmp_path, {
        "schema_version": 1,
        "queries": [
            {"query_id": "dup", "query": "x", "relevant": ["m"]},
            {"query_id": "dup", "query": "y", "relevant": ["n"]},
        ],
    })
    with pytest.raises(ValueError, match="duplicate"):
        load_qrels(p)


def test_load_qrels_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_qrels(tmp_path / "nope.json")


def test_load_qrels_expected_top1_not_in_relevant(tmp_path):
    """expected_top1 이 relevant 의 멤버가 아니면 ValueError(라벨 정합)."""
    p = _write_qrels(tmp_path, {"schema_version": 1, "queries": [
        {"query_id": "a", "query": "x", "relevant": ["m1"], "expected_top1": "m2"}]})
    with pytest.raises(ValueError, match="expected_top1"):
        load_qrels(p)


# ── T-B1b — 러너 리포트 생성 (mock recall, 결정적) ────────────────────
def _mock_recall_factory(mapping):
    """query→retrieved name 리스트 고정 매핑 mock recall_fn."""
    def _mock(query, top_k=5, score_threshold=0.0, raw_cosine_min=0.0,
             db_path=None, expand_wikilinks=False, use_ctx=False):
        names = mapping.get(query, [])
        return [{"name": n} for n in names[:top_k]]
    return _mock


def test_run_corpus_report_has_metric_keys(tmp_path):
    qrels = {
        "schema_version": 1,
        "queries": [
            {"query_id": "a", "query": "q-a", "relevant": ["m1"], "expected_top1": "m1"},
            {"query_id": "b", "query": "q-b", "relevant": ["m2"], "expected_top1": "m2"},
        ],
    }
    p = _write_qrels(tmp_path, qrels)
    mock = _mock_recall_factory({"q-a": ["m1", "x"], "q-b": ["y", "m2"]})
    report = run_corpus(p, k=5, recall_fn=mock, require_arctic=False)
    assert report["skipped"] is False
    metrics = report["metrics"]
    for key in ("mean_recall_at_5", "mean_precision_at_5", "mrr",
                "first_relevant_hit_rate", "expected_top1_hit_rate", "n"):
        assert key in metrics, key
    assert metrics["n"] == 2
    assert len(report["per_query"]) == 2
    # q-a: m1 top1 → first_hit 1, recall 1.0; q-b: m2 rank2 → first_hit 0, recall 1.0
    assert report["per_query"][0]["retrieved"] == ["m1", "x"]
    assert report["metrics"]["mean_recall_at_5"] == pytest.approx(1.0)
    assert report["metrics"]["first_relevant_hit_rate"] == pytest.approx(0.5)


# ── T-B4 — 러너 결정성 (fixture 인덱스, fake embed) ───────────────────
def _det_embed(text, kind="passage"):
    """프로세스 독립 결정적 임베딩 — hashlib 시드 numpy RandomState."""
    seed = int.from_bytes(
        hashlib.sha256(f"{kind}|{text}".encode("utf-8")).digest()[:4], "big"
    )
    rng = np.random.RandomState(seed)
    return rng.rand(1024).astype(np.float32).tolist()


def test_run_corpus_deterministic_twice(tmp_path):
    """동일 qrels·동일 index.db 로 run_corpus 2회 → per_query retrieved·메트릭 동일."""
    from memory_indexer import incremental_index

    fixture_src = REPO / "tests" / "fixtures" / "memory"
    mem_dir = tmp_path / "memory"
    shutil.copytree(fixture_src, mem_dir)
    db = tmp_path / "idx.db"
    with patch("memory_indexer.embed_text", side_effect=_det_embed):
        incremental_index([mem_dir], db_path=db)

    qrels = {
        "schema_version": 1,
        "queries": [
            {"query_id": "f1", "query": "메일 보내기 SMTP 발송", "relevant": ["test-mail"], "expected_top1": "test-mail"},
            {"query_id": "f2", "query": "스캐너 스캔 자동 크롭", "relevant": ["test-scanner"], "expected_top1": "test-scanner"},
        ],
    }
    p = _write_qrels(tmp_path, qrels)

    def _run():
        with patch("memory_search.embed_text", side_effect=_det_embed):
            return run_corpus(p, db_path=db, k=5, require_arctic=False)

    r1 = _run()
    r2 = _run()
    assert r1["skipped"] is False
    # 메트릭 dict 완전 동일
    assert r1["metrics"] == r2["metrics"]
    # per_query retrieved 순위·메트릭 동일 (latency 제외)
    for a, b in zip(r1["per_query"], r2["per_query"]):
        assert a["retrieved"] == b["retrieved"]
        assert a["recall_at_k"] == b["recall_at_k"]
        assert a["reciprocal_rank"] == b["reciprocal_rank"]
        assert a["first_relevant_hit"] == b["first_relevant_hit"]


def test_run_corpus_skip_when_arctic_down(tmp_path):
    """require_arctic=True + probe 실패 → skip 리포트 (B4 graceful)."""
    p = _write_qrels(tmp_path, {
        "schema_version": 1,
        "queries": [{"query_id": "a", "query": "x", "relevant": ["m"]}],
    })
    with patch("eval_runner._arctic_available", return_value=False):
        report = run_corpus(p, require_arctic=True)
    assert report["skipped"] is True
    assert "arctic" in report["reason"].lower()
