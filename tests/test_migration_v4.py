"""T-A1 — 스키마 v3→v4 마이그레이션 (CR 신컬럼). 멱등·데이터 보존·off 무영향.

goal A1/R4: ALTER ADD(nullable) 로 기존 동작 불변, 신컬럼은 회수 off 시 미사용.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import indexer
from indexer import SCHEMA_VERSION, _column_exists, _migrate_schema, open_db

# v4 이전(v3) 스키마 — 신컬럼 없는 원형
V3_DDL = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE memories (
    path TEXT PRIMARY KEY, name TEXT, description TEXT,
    mtime_ns INTEGER NOT NULL, indexed_at TEXT NOT NULL
);
CREATE TABLE memories_vec (
    rowid INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL, kind TEXT NOT NULL, embedding BLOB NOT NULL
);
"""

_EMB = bytes(range(64))  # 알려진 임베딩 바이트


def _make_v3_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(V3_DDL)
    conn.execute("INSERT INTO meta(key,value) VALUES('schema_version','3')")
    conn.execute(
        "INSERT INTO memories(path,name,description,mtime_ns,indexed_at) VALUES(?,?,?,?,?)",
        ("/m/a.md", "mem-a", "desc-a", 123, "2026-01-01"),
    )
    conn.execute(
        "INSERT INTO memories_vec(path,kind,embedding) VALUES(?,?,?)",
        ("/m/a.md", "body", _EMB),
    )
    conn.commit()
    conn.close()


def test_schema_version_is_4():
    assert SCHEMA_VERSION == 4


def test_v3_to_v4_adds_columns(tmp_path):
    db = tmp_path / "v3.db"
    _make_v3_db(db)
    conn = open_db(db)  # _init_db → _migrate_schema 경유
    try:
        assert _column_exists(conn, "memories_vec", "embedding_ctx")
        assert _column_exists(conn, "memories_vec", "cr_synopsis")
        assert _column_exists(conn, "memories", "cr_mode")
        assert _column_exists(conn, "memories", "corpus_generation")
        # 버전 기록 갱신
        ver = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        assert ver == str(SCHEMA_VERSION)
    finally:
        conn.close()


def test_migration_preserves_data(tmp_path):
    db = tmp_path / "v3.db"
    _make_v3_db(db)
    conn = open_db(db)
    try:
        # 행 개수 보존
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM memories_vec").fetchone()[0] == 1
        # embedding 바이트 동일(원본 불변)
        emb = conn.execute("SELECT embedding FROM memories_vec WHERE path='/m/a.md'").fetchone()[0]
        assert bytes(emb) == _EMB
        # 신컬럼은 기존 행에서 NULL (off 무영향)
        row = conn.execute(
            "SELECT cr_mode, corpus_generation FROM memories WHERE path='/m/a.md'"
        ).fetchone()
        assert row[0] is None and row[1] is None
        vrow = conn.execute(
            "SELECT embedding_ctx, cr_synopsis FROM memories_vec WHERE path='/m/a.md'"
        ).fetchone()
        assert vrow[0] is None and vrow[1] is None
    finally:
        conn.close()


def test_migration_idempotent_direct(tmp_path):
    """_migrate_schema 직접 2회 — column 가드로 에러 없음(meta 미persist 케이스)."""
    db = tmp_path / "v3.db"
    _make_v3_db(db)
    conn = sqlite3.connect(str(db))
    try:
        _migrate_schema(conn)  # 3 → 4 (메타는 안 씀)
        _migrate_schema(conn)  # 재실행 — current 여전히 3 이지만 가드로 중복 ADD 회피
        assert _column_exists(conn, "memories", "cr_mode")
        # 중복 컬럼 에러 없이 통과했으면 성공
    finally:
        conn.close()


def test_migration_idempotent_reopen(tmp_path):
    db = tmp_path / "v3.db"
    _make_v3_db(db)
    open_db(db).close()
    conn = open_db(db)  # 2회차 — 이미 v4
    try:
        assert _column_exists(conn, "memories", "corpus_generation")
        assert conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 1
    finally:
        conn.close()


def test_add_column_if_missing_swallows_duplicate_race(tmp_path):
    """check-then-ALTER race(다른 프로세스가 먼저 추가) 시 duplicate column 을 swallow.
    adversarial review 2026-06-17 R3."""
    from unittest.mock import patch
    from indexer import _add_column_if_missing

    db = tmp_path / "v3.db"
    _make_v3_db(db)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("ALTER TABLE memories ADD COLUMN cr_mode TEXT")  # 이미 추가됨
        # _column_exists 가 False 반환(race 모사) → ALTER 가 duplicate 던지나 swallow 돼야
        with patch("indexer._column_exists", return_value=False):
            _add_column_if_missing(conn, "memories", "cr_mode", "TEXT")  # 예외 없어야 함
        # 진짜 오류(없는 테이블)는 전파
        with patch("indexer._column_exists", return_value=False):
            try:
                _add_column_if_missing(conn, "no_such_table", "x", "TEXT")
                assert False, "real error should propagate"
            except sqlite3.OperationalError:
                pass
    finally:
        conn.close()


def test_concurrent_open_db_migration(tmp_path):
    """v3→v4 전환을 다중 스레드 동시 open_db 로 — 전부 성공·컬럼 정확히 1회(race 무crash)."""
    import threading

    db = tmp_path / "v3.db"
    _make_v3_db(db)
    errors: list = []

    def worker():
        try:
            c = open_db(db)
            c.close()
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"concurrent migration raised: {errors}"
    conn = open_db(db)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(memories)")]
        assert cols.count("cr_mode") == 1
        assert cols.count("corpus_generation") == 1
    finally:
        conn.close()


def test_fresh_db_has_new_columns(tmp_path):
    """신규 DB(CREATE TABLE 경로)도 신컬럼 보유 + 버전 4."""
    db = tmp_path / "fresh.db"
    conn = open_db(db)
    try:
        assert _column_exists(conn, "memories_vec", "embedding_ctx")
        assert _column_exists(conn, "memories_vec", "cr_synopsis")
        assert _column_exists(conn, "memories", "cr_mode")
        assert _column_exists(conn, "memories", "corpus_generation")
        ver = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        assert ver == "4"
    finally:
        conn.close()
