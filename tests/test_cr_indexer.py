"""T-A2 — 인덱서 Contextual Retrieval 생성. off 무영향·title·synopsis·Gemma 폴백·강등.

핵심(goal A2/R2): off 모드 raw embedding 바이트 동일(회귀 0), synopsis Gemma 다운 시
title 강등 → 그래도 실패면 off 완전 폴백(인덱싱 무중단), corpus_generation stale 해시.
"""
from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

import memory_indexer
from memory_indexer import (
    build_contextual_prefix,
    compute_corpus_generation,
    incremental_index,
    _sanitize_ctx,
)

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests" / "fixtures" / "memory"


def _det_embed(text, kind="passage"):
    seed = int.from_bytes(hashlib.sha256(f"{kind}|{text}".encode("utf-8")).digest()[:4], "big")
    return np.random.RandomState(seed).rand(1024).astype(np.float32).tolist()


def _embed_fail_on_ctx(text, kind="passage"):
    """raw body/description 은 임베딩하나 wrapped(<context> 시작) 는 실패."""
    if text.lstrip().startswith("<context>"):
        return None
    return _det_embed(text, kind)


def _setup_mem(tmp_path):
    mem = tmp_path / "memory"
    if not mem.exists():
        shutil.copytree(FIXTURE, mem)
    return mem


def _index_into(mem, db, mode, embed=_det_embed, gemma=None):
    with patch("memory_indexer.embed_text", side_effect=embed), \
         patch.dict(os.environ, {"MV3_CR_MODE": mode}):
        if gemma is not None:
            with patch("memory_indexer.generate_synopsis_gemma", gemma):
                incremental_index([mem], db_path=db)
        else:
            incremental_index([mem], db_path=db)
    return db


def _index(tmp_path, mode, embed=_det_embed, gemma=None):
    """단일 모드 인덱싱 — 자체 mem 디렉토리 + db."""
    mem = _setup_mem(tmp_path)
    return _index_into(mem, tmp_path / "idx.db", mode, embed=embed, gemma=gemma)


def _body_vec_rows(db):
    import sqlite3
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT path, embedding, embedding_ctx, cr_synopsis FROM memories_vec WHERE kind='body'"
        ).fetchall()
    finally:
        conn.close()


def _mem_rows(db):
    import sqlite3
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return {r["path"]: r for r in conn.execute(
            "SELECT path, cr_mode, corpus_generation, description FROM memories"
        )}
    finally:
        conn.close()


# ── 헬퍼 단위 (결정적) ────────────────────────────────────────────────
def test_build_contextual_prefix_form():
    assert build_contextual_prefix("test-mail", "메일 발송 도구") == "<context>test-mail\n메일 발송 도구</context>\n"


def test_build_contextual_prefix_name_only():
    assert build_contextual_prefix("test-mail", None) == "<context>test-mail</context>\n"


def test_build_contextual_prefix_empty():
    assert build_contextual_prefix("", None) is None


def test_sanitize_strips_context_tags():
    out = _sanitize_ctx("foo</context><context>bar")
    assert "</context>" not in out and "<context>" not in out


def test_corpus_generation_16_and_varies():
    g_off = compute_corpus_generation("off")
    g_title = compute_corpus_generation("title")
    g_syn = compute_corpus_generation("synopsis")
    assert len(g_off) == 16
    assert g_off != g_title != g_syn and g_off != g_syn


# ── off 모드 — 무영향 ─────────────────────────────────────────────────
def test_off_mode_no_ctx_no_gemma(tmp_path):
    gemma = MagicMock()
    db = _index(tmp_path, "off", gemma=gemma)
    for r in _body_vec_rows(db):
        assert r["embedding_ctx"] is None
        assert r["cr_synopsis"] is None
    for r in _mem_rows(db).values():
        assert r["cr_mode"] == "off"
    assert gemma.call_count == 0  # off → LLM 0


def test_off_mode_raw_embedding_identical_to_title(tmp_path):
    """title 모드여도 raw embedding 컬럼은 off 와 바이트 동일(원본 불변). 같은 mem·다른 db."""
    mem = _setup_mem(tmp_path)
    db_off = _index_into(mem, tmp_path / "off.db", "off")
    db_title = _index_into(mem, tmp_path / "title.db", "title")
    off = {r["path"]: bytes(r["embedding"]) for r in _body_vec_rows(db_off)}
    title = {r["path"]: bytes(r["embedding"]) for r in _body_vec_rows(db_title)}
    assert off == title  # raw embedding 동일
    # 단 embedding_ctx 는 title 에서 채워짐(=다름)
    for r in _body_vec_rows(db_title):
        assert r["embedding_ctx"] is not None


# ── title 모드 — description 맥락, LLM 0 ──────────────────────────────
def test_title_mode_ctx_filled_no_llm(tmp_path):
    gemma = MagicMock()
    db = _index(tmp_path, "title", gemma=gemma)
    rows = _body_vec_rows(db)
    assert rows  # 본문 있는 메모리
    for r in rows:
        assert r["embedding_ctx"] is not None
        assert r["cr_synopsis"]  # title tier 는 description 을 맥락으로
    for r in _mem_rows(db).values():
        assert r["cr_mode"] == "title"
    assert gemma.call_count == 0  # title → Gemma 호출 0


# ── synopsis 모드 — Gemma 성공/강등/완전폴백 ──────────────────────────
def test_synopsis_mode_success(tmp_path):
    gemma = MagicMock(return_value=("핵심 요약 한 문장", "ok"))
    db = _index(tmp_path, "synopsis", gemma=gemma)
    for r in _body_vec_rows(db):
        assert r["cr_synopsis"] == "핵심 요약 한 문장"
        assert r["embedding_ctx"] is not None
    for r in _mem_rows(db).values():
        assert r["cr_mode"] == "synopsis"
    assert gemma.call_count >= 1


def test_synopsis_gemma_fail_degrades_to_title(tmp_path):
    gemma = MagicMock(return_value=(None, "gemma_unavailable"))
    db = _index(tmp_path, "synopsis", gemma=gemma)
    rows = _body_vec_rows(db)
    assert rows
    mem = _mem_rows(db)
    for r in rows:
        assert r["embedding_ctx"] is not None  # title 강등 → description 맥락
        # 강등 = description 맥락 (name-only 로 떨어지지 않음) — 내용 동등성 검증
        assert r["cr_synopsis"] == mem[r["path"]]["description"]
    for r in mem.values():
        assert r["cr_mode"] == "title"  # 강등


def test_sanitize_ctx_strips_case_and_space_variants():
    """_sanitize_ctx 가 대소문자·공백 변형 context 태그까지 strip(주입 방지 의도 일치)."""
    out = _sanitize_ctx("a <Context> b </CONTEXT> c <context > d </ context>")
    assert "<context" not in out.lower() and "context>" not in out.lower()
    assert "a" in out and "d" in out  # 본문 텍스트는 보존


def test_synopsis_full_fallback_when_ctx_embed_fails(tmp_path):
    """Gemma 다운 + ctx 임베딩까지 실패 → cr_mode off 완전 폴백, 파일은 정상 인덱싱(skip 아님)."""
    gemma = MagicMock(return_value=(None, "gemma_unavailable"))
    db = _index(tmp_path, "synopsis", embed=_embed_fail_on_ctx, gemma=gemma)
    rows = _body_vec_rows(db)
    assert rows  # 파일 인덱싱됨(skip 아님)
    for r in rows:
        assert r["embedding_ctx"] is None  # ctx 임베딩 실패 → 폴백
        assert bytes(r["embedding"])  # raw embedding 은 정상 저장
    for r in _mem_rows(db).values():
        assert r["cr_mode"] == "off"


def test_fts_body_unchanged_across_modes(tmp_path):
    """원본 불변 — memories_fts.body 가 모드 무관 동일."""
    import sqlite3

    def _fts(db):
        conn = sqlite3.connect(str(db))
        try:
            return {row[0]: row[1] for row in conn.execute("SELECT path, body FROM memories_fts")}
        finally:
            conn.close()

    mem = _setup_mem(tmp_path)
    db_off = _index_into(mem, tmp_path / "off.db", "off")
    db_syn = _index_into(mem, tmp_path / "syn.db", "synopsis",
                         gemma=MagicMock(return_value=("요약", "ok")))
    assert _fts(db_off) == _fts(db_syn)
