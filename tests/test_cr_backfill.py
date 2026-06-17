"""T-A4 — CR 백필 CLI. stale 감지·재개·dry-run·멱등·Gemma 호출당 1회."""
from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

import cr_backfill_cli
from cr_backfill_cli import cr_backfill, main
from memory_indexer import compute_corpus_generation, incremental_index

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests" / "fixtures" / "memory"


def _det_embed(text, kind="passage"):
    seed = int.from_bytes(hashlib.sha256(f"{kind}|{text}".encode("utf-8")).digest()[:4], "big")
    return np.random.RandomState(seed).rand(1024).astype(np.float32).tolist()


def _build_off_index(tmp_path):
    mem = tmp_path / "memory"
    shutil.copytree(FIXTURE, mem)
    db = tmp_path / "idx.db"
    with patch("memory_indexer.embed_text", side_effect=_det_embed), \
         patch.dict(os.environ, {"MV3_CR_MODE": "off"}):
        incremental_index([mem], db_path=db)
    return db


def _ctx_state(db):
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        vec = conn.execute(
            "SELECT path, embedding_ctx FROM memories_vec WHERE kind='body'").fetchall()
        mem = conn.execute("SELECT path, cr_mode, corpus_generation FROM memories").fetchall()
        return vec, {m["path"]: m for m in mem}
    finally:
        conn.close()


# ── stale 감지 + 재처리 ───────────────────────────────────────────────
def test_backfill_processes_stale_then_converges(tmp_path):
    db = _build_off_index(tmp_path)
    with patch("memory_indexer.embed_text", side_effect=_det_embed):
        c1 = cr_backfill("title", db_path=db)
    assert c1["candidates"] == 3 and c1["processed"] == 3  # off → 전부 stale
    vec, mem = _ctx_state(db)
    for r in vec:
        assert r["embedding_ctx"] is not None  # ctx 채워짐
    title_gen = compute_corpus_generation("title")
    for m in mem.values():
        assert m["corpus_generation"] == title_gen
        assert m["cr_mode"] == "title"
    # 재실행 → 전부 일치 → 0 candidates (수렴/멱등)
    with patch("memory_indexer.embed_text", side_effect=_det_embed):
        c2 = cr_backfill("title", db_path=db)
    assert c2["candidates"] == 0 and c2["processed"] == 0


def test_backfill_dry_run_no_writes(tmp_path):
    db = _build_off_index(tmp_path)
    off_gen = compute_corpus_generation("off")
    embed_spy = MagicMock(side_effect=_det_embed)
    with patch("memory_indexer.embed_text", embed_spy):
        c = cr_backfill("title", db_path=db, dry_run=True)
    assert c["dry_run"] is True
    assert c["candidates"] == 3 and c["processed"] == 3  # 대상 리스트는 셈
    vec, mem = _ctx_state(db)
    for r in vec:
        assert r["embedding_ctx"] is None  # 쓰기 0
    for m in mem.values():
        assert m["corpus_generation"] == off_gen  # 갱신 안 됨
    assert embed_spy.call_count == 0  # dry-run 은 임베딩 0


def test_backfill_resume_after_interrupt(tmp_path):
    db = _build_off_index(tmp_path)
    with patch("memory_indexer.embed_text", side_effect=_det_embed):
        c1 = cr_backfill("title", db_path=db, limit=1)  # 1건만(중단 모사)
    assert c1["processed"] == 1
    with patch("memory_indexer.embed_text", side_effect=_det_embed):
        c2 = cr_backfill("title", db_path=db)  # 나머지
    assert c2["candidates"] == 2 and c2["processed"] == 2  # 이미 처리분 skip
    # 전부 처리됨
    _, mem = _ctx_state(db)
    title_gen = compute_corpus_generation("title")
    assert all(m["corpus_generation"] == title_gen for m in mem.values())


def test_backfill_synopsis_gemma_per_memory(tmp_path):
    db = _build_off_index(tmp_path)
    gemma = MagicMock(return_value=("요약 한 문장", "ok"))
    with patch("memory_indexer.embed_text", side_effect=_det_embed), \
         patch("memory_indexer.generate_synopsis_gemma", gemma):
        c = cr_backfill("synopsis", db_path=db)
    assert c["processed"] == 3
    assert gemma.call_count == 3  # 메모리당 1회
    _, mem = _ctx_state(db)
    assert all(m["cr_mode"] == "synopsis" for m in mem.values())


def test_backfill_synopsis_gemma_degrade_stays_candidate(tmp_path):
    """synopsis 모드 Gemma 일시중단→title 강등 시 gen(title) 마킹(gen(synopsis) 아님) →
    다음 백필 후보로 남아 Gemma 복구 후 synopsis 도달(영구 title 고정 차단). R12."""
    db = _build_off_index(tmp_path)
    syn_gen = compute_corpus_generation("synopsis")
    title_gen = compute_corpus_generation("title")
    # Gemma 다운(강등) — Arctic embed 는 정상
    gemma_down = MagicMock(return_value=(None, "gemma_unavailable"))
    with patch("memory_indexer.embed_text", side_effect=_det_embed), \
         patch("memory_indexer.generate_synopsis_gemma", gemma_down):
        c1 = cr_backfill("synopsis", db_path=db)
    assert c1["processed"] >= 1
    _, mem = _ctx_state(db)
    for m in mem.values():
        assert m["cr_mode"] == "title"  # 강등
        assert m["corpus_generation"] == title_gen  # gen(title), gen(synopsis) 아님
        assert m["corpus_generation"] != syn_gen
    # 여전히 synopsis 백필 후보 → Gemma 복구 후 synopsis 도달·수렴
    gemma_up = MagicMock(return_value=("진짜 요약 한 문장", "ok"))
    with patch("memory_indexer.embed_text", side_effect=_det_embed), \
         patch("memory_indexer.generate_synopsis_gemma", gemma_up):
        c2 = cr_backfill("synopsis", db_path=db)
    assert c2["processed"] >= 1  # 후보로 재시도됨
    _, mem2 = _ctx_state(db)
    assert all(m["cr_mode"] == "synopsis" for m in mem2.values())
    assert all(m["corpus_generation"] == syn_gen for m in mem2.values())  # 수렴


def test_indexer_synopsis_gemma_degrade_stays_candidate(tmp_path):
    """인덱서 synopsis 모드 Gemma 중단→title 강등 시 gen(title) 마킹 → 백필 후보 유지. R12."""
    mem = tmp_path / "memory"
    shutil.copytree(FIXTURE, mem)
    db = tmp_path / "idx.db"
    gemma_down = MagicMock(return_value=(None, "gemma_unavailable"))
    with patch("memory_indexer.embed_text", side_effect=_det_embed), \
         patch("memory_indexer.generate_synopsis_gemma", gemma_down), \
         patch.dict(os.environ, {"MV3_CR_MODE": "synopsis"}):
        incremental_index([mem], db_path=db)
    _, mem_rows = _ctx_state(db)
    title_gen = compute_corpus_generation("title")
    syn_gen = compute_corpus_generation("synopsis")
    for m in mem_rows.values():
        assert m["cr_mode"] == "title"  # 강등
        assert m["corpus_generation"] == title_gen  # gen(title), gen(synopsis) 아님
    # synopsis 백필이 후보로 잡아 재시도
    gemma_up = MagicMock(return_value=("요약", "ok"))
    with patch("memory_indexer.embed_text", side_effect=_det_embed), \
         patch("memory_indexer.generate_synopsis_gemma", gemma_up):
        c = cr_backfill("synopsis", db_path=db)
    assert c["processed"] >= 1
    _, mem2 = _ctx_state(db)
    assert all(m["corpus_generation"] == syn_gen for m in mem2.values())


def test_backfill_synopsis_dry_run_no_gemma(tmp_path):
    db = _build_off_index(tmp_path)
    gemma = MagicMock(return_value=("요약", "ok"))
    with patch("memory_indexer.embed_text", side_effect=_det_embed), \
         patch("memory_indexer.generate_synopsis_gemma", gemma):
        cr_backfill("synopsis", db_path=db, dry_run=True)
    assert gemma.call_count == 0  # dry-run → Gemma 0


def _vec_by_path(db):
    return {r["path"]: r for r in _ctx_state(db)[0]}


def test_offmode_reindex_metadata_touch_preserves_ctx(tmp_path):
    """body 불변(mtime touch만) off-reindex → ctx 벡터 보존(R1: 백필 결과 파괴 금지)."""
    import os as _os
    mem = tmp_path / "memory"
    shutil.copytree(FIXTURE, mem)
    db = tmp_path / "idx.db"
    with patch("memory_indexer.embed_text", side_effect=_det_embed), \
         patch.dict(os.environ, {"MV3_CR_MODE": "off"}):
        incremental_index([mem], db_path=db)
    with patch("memory_indexer.embed_text", side_effect=_det_embed):
        cr_backfill("title", db_path=db)
    # body 안 바꾸고 mtime 만 bump → off-reindex (content 동일)
    target = mem / "feedback_test_mail.md"
    st = target.stat()
    _os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    with patch("memory_indexer.embed_text", side_effect=_det_embed), \
         patch.dict(os.environ, {"MV3_CR_MODE": "off"}):
        incremental_index([mem], db_path=db)
    vec = _vec_by_path(db)
    assert all(r["embedding_ctx"] is not None for r in vec.values()), "body 불변인데 ctx 파괴됨"


def test_offmode_reindex_body_change_invalidates_stale_ctx(tmp_path):
    """body 변경 off-reindex → stale ctx 무효화(NULL) → use_ctx 랭킹 오염 차단(codex R11).
    gen(off) 로 남아 다음 백필이 새 body 로 refresh."""
    import os as _os
    mem = tmp_path / "memory"
    shutil.copytree(FIXTURE, mem)
    db = tmp_path / "idx.db"
    with patch("memory_indexer.embed_text", side_effect=_det_embed), \
         patch.dict(os.environ, {"MV3_CR_MODE": "off"}):
        incremental_index([mem], db_path=db)
    with patch("memory_indexer.embed_text", side_effect=_det_embed):
        cr_backfill("title", db_path=db)
    # body 내용 변경 → off-reindex
    target = mem / "feedback_test_mail.md"
    target.write_text(target.read_text(encoding="utf-8") + "\n새 본문 추가\n", encoding="utf-8")
    st = target.stat()
    _os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    with patch("memory_indexer.embed_text", side_effect=_det_embed), \
         patch.dict(os.environ, {"MV3_CR_MODE": "off"}):
        incremental_index([mem], db_path=db)

    tgt = str(target)
    off_gen = compute_corpus_generation("off")
    title_gen = compute_corpus_generation("title")
    vec = _vec_by_path(db)
    _, mem_rows = _ctx_state(db)
    # 편집 메모리: stale ctx 무효화(NULL) + gen(off)
    assert vec[tgt]["embedding_ctx"] is None, "body 변경된 stale ctx 가 보존됨(오염)"
    assert mem_rows[tgt]["corpus_generation"] == off_gen
    # 편집 안 된 메모리는 ctx 유지(reindex 안 됨)
    others = [p for p in vec if p != tgt]
    assert all(vec[p]["embedding_ctx"] is not None for p in others)
    # 다음 title 백필이 편집 메모리를 refresh(새 body 로 ctx 재생성)
    with patch("memory_indexer.embed_text", side_effect=_det_embed):
        c = cr_backfill("title", db_path=db)
    assert c["processed"] >= 1
    assert _vec_by_path(db)[tgt]["embedding_ctx"] is not None  # refresh 됨


def test_backfill_skips_raw_stale_memory(tmp_path):
    """raw 인덱스 stale(파일 편집 후 incremental_index 미실행)면 백필 skip → ctx/raw
    skew + 가짜 converged 방지(codex 2-track R11)."""
    import os as _os
    mem = tmp_path / "memory"
    shutil.copytree(FIXTURE, mem)
    db = tmp_path / "idx.db"
    with patch("memory_indexer.embed_text", side_effect=_det_embed), \
         patch.dict(os.environ, {"MV3_CR_MODE": "off"}):
        incremental_index([mem], db_path=db)
    # 파일 편집(내용+mtime) 하되 incremental_index 는 안 돌림 → raw stale
    target = mem / "feedback_test_mail.md"
    target.write_text(target.read_text(encoding="utf-8") + "\n편집됨\n", encoding="utf-8")
    st = target.stat()
    _os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
    with patch("memory_indexer.embed_text", side_effect=_det_embed):
        c = cr_backfill("title", db_path=db)
    assert c["skipped_stale"] >= 1  # stale 메모리 skip
    # stale 메모리엔 ctx 안 씀(skew 안 생김)
    assert _vec_by_path(db)[str(target)]["embedding_ctx"] is None


def test_backfill_skips_marker_on_embed_failure(tmp_path):
    """임베딩 서버 다운으로 ctx 생성 실패 시 corpus_generation 마커를 쓰지 않음 →
    다음 백필 후보로 남아 재시도(영구 sentinel 차단). adversarial review 2026-06-17 R2."""
    db = _build_off_index(tmp_path)
    title_gen = compute_corpus_generation("title")
    # 임베딩 다운 모사 — embed_text 가 None (ctx 생성 실패)
    with patch("memory_indexer.embed_text", side_effect=lambda *a, **k: None):
        c = cr_backfill("title", db_path=db)
    assert c["failed_embed"] >= 1
    assert c["processed"] == 0  # 마커 미기록(converged 표시 안 함)
    _, mem = _ctx_state(db)
    # corpus_generation 이 title 로 안 바뀜 → 후보로 남음(재시도 가능)
    assert all(m["corpus_generation"] != title_gen for m in mem.values())
    # 서버 복구 후 재시도 → 정상 처리
    with patch("memory_indexer.embed_text", side_effect=_det_embed):
        c2 = cr_backfill("title", db_path=db)
    assert c2["processed"] >= 1
    _, mem2 = _ctx_state(db)
    assert all(m["corpus_generation"] == title_gen for m in mem2.values())


def _embed_fail_on_ctx(text, kind="passage"):
    """raw body/description 은 임베딩하나 wrapped(<context> 시작) 는 실패."""
    if text.lstrip().startswith("<context>"):
        return None
    return _det_embed(text, kind)


def test_indexer_embed_failure_keeps_backfill_candidate(tmp_path):
    """MV3_CR_MODE=title 인덱싱 중 ctx 임베딩 실패 → corpus_generation=gen(off)(converged
    마커 금지) → 서버 복구 후 cr_backfill 후보로 남아 재시도. adversarial review 2026-06-17 R5."""
    mem = tmp_path / "memory"
    shutil.copytree(FIXTURE, mem)
    db = tmp_path / "idx.db"
    # title 모드인데 ctx(wrapped) 임베딩만 실패(raw body/desc 성공)
    with patch("memory_indexer.embed_text", side_effect=_embed_fail_on_ctx), \
         patch.dict(os.environ, {"MV3_CR_MODE": "title"}):
        incremental_index([mem], db_path=db)
    _, mem_rows = _ctx_state(db)
    off_gen = compute_corpus_generation("off")
    title_gen = compute_corpus_generation("title")
    # ctx 실패 → cr_mode=off + corpus_generation=off(마커 미기록) → 백필 후보
    for m in mem_rows.values():
        assert m["cr_mode"] == "off"
        assert m["corpus_generation"] == off_gen  # gen(title) 로 굳지 않음
    # 서버 복구 후 백필 → 후보로 잡혀 ctx 채워짐(영구 손실 아님)
    with patch("memory_indexer.embed_text", side_effect=_det_embed):
        c = cr_backfill("title", db_path=db)
    assert c["processed"] >= 1
    vec, mem2 = _ctx_state(db)
    assert all(r["embedding_ctx"] is not None for r in vec)
    assert all(m["corpus_generation"] == title_gen for m in mem2.values())


def test_backfill_aborts_when_indexer_lock_held(tmp_path):
    """백필이 memory-indexer.lock 으로 인덱서와 직렬화 — 락 보유 중이면 abort(lost-update
    race 차단). adversarial review 2026-06-17 R8."""
    from memory_indexer import _acquire_lock, _release_lock

    db = _build_off_index(tmp_path)
    held = _acquire_lock(db)  # 동시 인덱서가 락 보유 모사
    assert held is not None
    try:
        with patch("memory_indexer.embed_text", side_effect=_det_embed):
            c = cr_backfill("title", db_path=db)
        assert c.get("lock_busy") is True
        assert c["processed"] == 0  # 락 못 잡아 아무것도 안 함
    finally:
        _release_lock(held)
    # 락 해제 후엔 정상 처리(직렬화 후 진행)
    with patch("memory_indexer.embed_text", side_effect=_det_embed):
        c2 = cr_backfill("title", db_path=db)
    assert c2.get("lock_busy") is False
    assert c2["processed"] >= 1


def test_backfill_empty_body_converges(tmp_path):
    """빈 body(frontmatter-only) 메모리는 설정모드 기준 수렴 마킹 → 매 run 재선정되는
    무한 no-op 방지(R13: gen(effective)=gen(off)≠target_gen 비수렴 회귀 차단)."""
    mem = tmp_path / "memory"
    shutil.copytree(FIXTURE, mem)
    # frontmatter-only(body 빈) 메모리 추가
    (mem / "empty_body.md").write_text(
        "---\nname: empty-mem\ndescription: 본문 없는 메모리\n---\n", encoding="utf-8")
    db = tmp_path / "idx.db"
    with patch("memory_indexer.embed_text", side_effect=_det_embed), \
         patch.dict(os.environ, {"MV3_CR_MODE": "off"}):
        incremental_index([mem], db_path=db)
    # title 백필 2회 → 2회차 candidates=0 (빈-body 포함 전부 수렴)
    with patch("memory_indexer.embed_text", side_effect=_det_embed):
        cr_backfill("title", db_path=db)
        c2 = cr_backfill("title", db_path=db)
    assert c2["candidates"] == 0, f"빈-body 비수렴: 2회차 candidates={c2['candidates']}"
    # synopsis 백필도 동일 수렴(빈-body 가 영구 후보 아님)
    with patch("memory_indexer.embed_text", side_effect=_det_embed), \
         patch("memory_indexer.generate_synopsis_gemma", MagicMock(return_value=("요약", "ok"))):
        cr_backfill("synopsis", db_path=db)
        c4 = cr_backfill("synopsis", db_path=db)
    assert c4["candidates"] == 0


def test_backfill_invalid_mode(tmp_path):
    db = _build_off_index(tmp_path)
    import pytest
    with pytest.raises(ValueError, match="mode"):
        cr_backfill("off", db_path=db)


# ── CLI ───────────────────────────────────────────────────────────────
def test_cli_requires_flag(tmp_path, capsys):
    import pytest
    with pytest.raises(SystemExit):
        main(["--mode", "title"])  # --cr-backfill 없음 → argparse error


def test_cli_dry_run(tmp_path):
    db = _build_off_index(tmp_path)
    with patch("cr_backfill_cli.open_db", side_effect=lambda *a, **k: __import__("indexer").open_db(db)), \
         patch("memory_indexer.embed_text", side_effect=_det_embed):
        rc = main(["--cr-backfill", "--mode", "title", "--dry-run"])
    assert rc == 0
