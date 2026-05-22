"""Sprint 4 Task 2 — schema V2 마이그레이션 검증."""
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from indexer import open_db, SCHEMA_VERSION


class TestSchemaV2(unittest.TestCase):
    def test_schema_version_is_2(self):
        self.assertEqual(SCHEMA_VERSION, 2)

    def test_all_tables_exist_in_fresh_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            conn = open_db(db)
            try:
                names = {
                    r[0] for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                    )
                }
                # Sprint 1~3 (보존)
                self.assertIn("sessions", names)
                self.assertIn("sessions_fts", names)
                # Sprint 4 (신규)
                self.assertIn("memories", names)
                self.assertIn("memories_fts", names)
                self.assertIn("memories_vec", names)
            finally:
                conn.close()

    def test_v1_db_migrates_to_v2_preserving_sessions(self):
        """V1 스키마로 만든 DB가 V2로 마이그레이션돼도 sessions 데이터 보존."""
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            # V1 스키마로 수동 생성 + sessions row 1개 삽입
            c = sqlite3.connect(str(db))
            c.executescript(
                """
                CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
                INSERT INTO meta VALUES('schema_version', '1');
                CREATE TABLE sessions(
                    session_id TEXT PRIMARY KEY,
                    file_path TEXT NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    first_ts TEXT,
                    last_ts TEXT,
                    turn_count INTEGER,
                    indexed_at TEXT NOT NULL
                );
                INSERT INTO sessions VALUES(
                    'preserve-me', '/x', 1, 1, '2026-01-01', '2026-01-02', 1, '2026-01-03'
                );
                CREATE VIRTUAL TABLE sessions_fts USING fts5(
                    session_id UNINDEXED,
                    body,
                    tokenize = 'unicode61 remove_diacritics 2'
                );
                INSERT INTO sessions_fts(session_id, body) VALUES('preserve-me', 'old body');
                """
            )
            c.commit()
            c.close()

            # open_db로 마이그레이션 트리거
            conn = open_db(db)
            try:
                # version 2로 bump
                ver = conn.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()[0]
                self.assertEqual(ver, "2")
                # sessions 데이터 보존
                row = conn.execute(
                    "SELECT session_id FROM sessions WHERE session_id='preserve-me'"
                ).fetchone()
                self.assertIsNotNone(row, "sessions row should be preserved")
                # sessions_fts 데이터 보존
                fts = conn.execute(
                    "SELECT session_id FROM sessions_fts WHERE session_id='preserve-me'"
                ).fetchone()
                self.assertIsNotNone(fts, "sessions_fts row should be preserved")
                # memories_* 테이블 신규 생성
                tables = {
                    r[0] for r in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
                    )
                }
                self.assertIn("memories", tables)
                self.assertIn("memories_fts", tables)
            finally:
                conn.close()

    def test_memories_vec_accepts_1024d_vector_blob(self):
        """memories_vec BLOB 컬럼에 1024 float32 임베딩 삽입/조회."""
        import numpy as np
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "test.db"
            conn = open_db(db)
            try:
                vec = np.full(1024, 0.1, dtype=np.float32)
                conn.execute(
                    "INSERT INTO memories_vec(path, kind, embedding) VALUES(?,?,?)",
                    ("/x.md", "body", vec.tobytes()),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT kind, path, embedding FROM memories_vec WHERE path='/x.md'"
                ).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row["kind"], "body")
                restored = np.frombuffer(row["embedding"], dtype=np.float32)
                self.assertEqual(restored.shape, (1024,))
                self.assertAlmostEqual(float(restored[0]), 0.1, places=5)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
