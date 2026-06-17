"""T-A3 — 검색 Contextual Retrieval 경로. off 회귀 0 + ctx 사용 + COALESCE 폴백.

goal A1/A3: MV3_CR_SEARCH off(기본)면 기존 회수 동작 불변(회귀 0). on 이면 body 행이
ctx 임베딩 기준 코사인, ctx NULL(off-인덱싱) 메모리는 raw 폴백(누락 0). 쿼리는 clean.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

import memory_search
from memory_search import _cr_search_enabled, _vec_top_k, recall_memory
from memory_indexer import incremental_index
from indexer import open_db

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests" / "fixtures" / "memory"


def _det_embed(text, kind="passage"):
    seed = int.from_bytes(hashlib.sha256(f"{kind}|{text}".encode("utf-8")).digest()[:4], "big")
    return np.random.RandomState(seed).rand(1024).astype(np.float32).tolist()


def _build_index(tmp_path, mode):
    mem = tmp_path / "memory"
    shutil.copytree(FIXTURE, mem)
    db = tmp_path / "idx.db"
    with patch("memory_indexer.embed_text", side_effect=_det_embed), \
         patch.dict(os.environ, {"MV3_CR_MODE": mode}):
        incremental_index([mem], db_path=db)
    return db


# ── env 게이트 ────────────────────────────────────────────────────────
def test_cr_search_disabled_by_default():
    with patch.dict(os.environ):
        os.environ.pop("MV3_CR_SEARCH", None)
        assert _cr_search_enabled() is False
        os.environ["MV3_CR_SEARCH"] = "1"
        assert _cr_search_enabled() is True
        os.environ["MV3_CR_SEARCH"] = "0"
        assert _cr_search_enabled() is False


# ── _vec_top_k ctx 사용 ───────────────────────────────────────────────
def test_vec_top_k_ctx_ranks_but_raw_gates(tmp_path):
    """use_ctx=True: body 행은 ctx 로 *랭킹*, raw 로 *게이트*. adversarial review R14 —
    0.32 raw 게이트는 raw 분포로 캘리브됐으므로 ctx 코사인에 적용하면 미스게이팅."""
    db = _build_index(tmp_path, "title")  # embedding_ctx 채워짐
    conn = open_db(db)
    try:
        qv = _det_embed("메일 발송 SMTP", "query")
        body_paths = [r["path"] for r in conn.execute(
            "SELECT path FROM memories_vec WHERE kind='body' AND embedding_ctx IS NOT NULL")]
        assert body_paths
        # (게이트=raw) use_ctx True/False 의 raw_map(body) 동일 — 게이트는 raw 기준
        _, raw_off = _vec_top_k(conn, qv, use_ctx=False)
        _, raw_ctx = _vec_top_k(conn, qv, use_ctx=True)
        for p in body_paths:
            assert abs(raw_off.get(p, 0) - raw_ctx.get(p, 0)) < 1e-6, "게이트가 raw 아님"

        # (랭킹=ctx) 한 body 의 ctx 를 쿼리와 동일(max sim)로 세팅 → use_ctx 면 rank #1
        target = body_paths[0]
        conn.execute("UPDATE memories_vec SET embedding_ctx=? WHERE kind='body' AND path=?",
                     (np.asarray(qv, dtype=np.float32).tobytes(), target))
        conn.commit()
        res_on, _ = _vec_top_k(conn, qv, use_ctx=True, limit=10)
        res_off, _ = _vec_top_k(conn, qv, use_ctx=False, limit=10)
        # body 행만(같은 path 의 description 행이 dict 덮어쓰지 않게 kind 필터)
        on_rank = {p: rk for p, rk, k in res_on if k == "body"}
        off_rank = {p: rk for p, rk, k in res_off if k == "body"}
        assert on_rank[target] == 1, "ctx==query 인데 use_ctx 랭킹 1위 아님(ctx 미반영)"
        assert on_rank[target] <= off_rank.get(target, 999)  # off 는 raw 기준(ctx 무시)
    finally:
        conn.close()


def test_vec_top_k_corrupt_ctx_falls_back_to_raw(tmp_path):
    """손상된 embedding_ctx(non-NULL)도 같은 행의 raw embedding 으로 폴백(누락 0).
    adversarial review 2026-06-17: COALESCE 가 손상 ctx 를 골라 행을 통째 skip 하던 버그 회귀."""
    db = _build_index(tmp_path, "title")
    conn = open_db(db)
    try:
        target = conn.execute(
            "SELECT path FROM memories_vec WHERE kind='body' LIMIT 1").fetchone()["path"]
        # 3바이트 손상 ctx 주입 (frombuffer ValueError 유발 — 4의 배수 아님)
        conn.execute("UPDATE memories_vec SET embedding_ctx=? WHERE kind='body' AND path=?",
                     (b"\x00\x01\x02", target))
        conn.commit()
        qv = _det_embed("메일", "query")
        _, raw_off = _vec_top_k(conn, qv, use_ctx=False)
        _, raw_ctx = _vec_top_k(conn, qv, use_ctx=True)
        # 손상 ctx → raw 폴백 → target 누락 0, raw 코사인과 동일
        assert target in raw_ctx, "손상 ctx 행이 raw 폴백 없이 사라짐(누락 회귀)"
        assert raw_ctx[target] == raw_off[target]
    finally:
        conn.close()


def test_vec_top_k_coalesce_fallback_on_null_ctx(tmp_path):
    db = _build_index(tmp_path, "title")
    conn = open_db(db)
    try:
        # 한 body 의 ctx 를 NULL 로 → off-인덱싱 메모리 모사
        target = conn.execute(
            "SELECT path FROM memories_vec WHERE kind='body' LIMIT 1").fetchone()["path"]
        conn.execute("UPDATE memories_vec SET embedding_ctx=NULL WHERE kind='body' AND path=?", (target,))
        conn.commit()
        qv = _det_embed("메일", "query")
        _, raw_off = _vec_top_k(conn, qv, use_ctx=False)
        _, raw_ctx = _vec_top_k(conn, qv, use_ctx=True)
        # COALESCE 로 raw 폴백 → 누락 0 + 코사인은 raw 와 동일
        assert target in raw_ctx
        assert raw_ctx[target] == raw_off[target]
    finally:
        conn.close()


# ── recall off 회귀 0 ─────────────────────────────────────────────────
def test_recall_use_ctx_false_ignores_ctx(tmp_path):
    """use_ctx=False 면 raw embedding 으로만 회수(회귀 0), use_ctx=True 는 ctx 반영.
    통합 레벨에서 off 의 raw_cosine 이 raw embedding 직접 코사인과 일치하고(=ctx 무시),
    off↔on raw_cosine 이 최소 한 path 에서 다름(=ctx 적용)을 직접 assert."""
    db = _build_index(tmp_path, "title")
    with patch("memory_search.embed_text", side_effect=lambda q: _det_embed(q, "query")):
        res_off = recall_memory("메일 발송", top_k=3, score_threshold=0.0, db_path=db, use_ctx=False)
        res_on = recall_memory("메일 발송", top_k=3, score_threshold=0.0, db_path=db, use_ctx=True)
    assert isinstance(res_off, list) and res_off
    assert isinstance(res_on, list) and res_on

    # (1) off 의 raw_cosine == raw embedding 직접 코사인 (off 가 ctx 안 씀을 검증)
    qv = np.asarray(_det_embed("메일 발송", "query"), dtype=np.float32)
    qv = qv / np.linalg.norm(qv)
    conn = open_db(db)
    try:
        for r in res_off:
            rows = conn.execute(
                "SELECT embedding FROM memories_vec WHERE path=? AND embedding IS NOT NULL",
                (r["path"],)).fetchall()
            expected = max(
                float(np.frombuffer(row["embedding"], dtype=np.float32) @ qv
                      / np.linalg.norm(np.frombuffer(row["embedding"], dtype=np.float32)))
                for row in rows)
            assert abs(r["raw_cosine"] - round(expected, 4)) < 1e-3
    finally:
        conn.close()

    # (2) gate-by-raw(R14): off↔on raw_cosine 동일 — use_ctx 는 *랭킹*만 ctx, *게이트*는
    #     raw 기준이라 raw_cosine(게이트값)은 안 바뀐다(0.32 게이트 미스캘리 방지).
    off_raw = {r["path"]: r["raw_cosine"] for r in res_off}
    on_raw = {r["path"]: r["raw_cosine"] for r in res_on}
    common = off_raw.keys() & on_raw.keys()
    assert common
    for p in common:
        assert abs(off_raw[p] - on_raw[p]) < 1e-3, \
            "게이트가 raw 아님 — use_ctx 가 raw_cosine(게이트값)을 바꿈(R14 회귀)"


def test_query_embedding_is_clean(tmp_path):
    """쿼리 임베딩은 wrapper(<context>) 미적용 — 받은 인자가 원 쿼리."""
    db = _build_index(tmp_path, "title")
    spy = MagicMock(side_effect=lambda q: _det_embed(q, "query"))
    with patch("memory_search.embed_text", spy):
        recall_memory("메일 발송 SMTP", top_k=3, score_threshold=0.0, db_path=db, use_ctx=True)
    assert spy.call_count >= 1
    called_arg = spy.call_args[0][0]
    assert "<context>" not in called_arg
    assert called_arg == "메일 발송 SMTP"
