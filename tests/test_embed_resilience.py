"""bug-audit 2026-05-29 회귀 테스트 — 임베딩 서버 일시 장애가 영구 데이터 유실로
번지지 않음을 검증.

커버하는 수정:
- indexing-wal-missing-1: open_db 가 journal_mode=WAL 설정.
- indexing-embed-fail-permanent-sentinel-1: backfill_session_vecs 가 임베딩 서버
  장애 시 영구 빈-blob sentinel 을 박지 않고, 서버 복구 후 재시도해 채운다.
- embeddings-alias-1 / embeddings-alias-6: memory incremental_index 가 임베딩
  실패 시 mtime 을 갱신하지 않아 다음 run 이 재시도한다 (영구 vec 누락 방지).
"""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def _fake_embed(_text, *args, **kwargs):
    return [0.5] * 1024


def _embed_down(*_args, **_kwargs):
    # 서버 다운: embed_text 가 None 을 반환하는 상황
    return None


class TestOpenDbWAL(unittest.TestCase):
    def test_open_db_sets_wal(self):
        from indexer import open_db
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "index.db"
            conn = open_db(db)
            try:
                mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(mode.lower(), "wal")


class TestSessionIndexerLock(unittest.TestCase):
    """indexing-session-indexer-no-lock-1: 동시 세션 인덱서 직렬화."""

    def test_lock_busy_skips_second_indexer(self):
        import indexer
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "index.db"
            held = indexer._acquire_session_lock(db)
            self.assertIsNotNone(held)
            try:
                # 락이 잡힌 동안 incremental_index 는 즉시 0 으로 skip
                projects = Path(d) / "projects"
                (projects / "slot").mkdir(parents=True)
                (projects / "slot" / "s.jsonl").write_text(
                    '{"type":"user","message":{"content":"본문 충분히 김"},"timestamp":"2026-01-01T00:00:00"}\n'
                )
                rc = indexer.incremental_index(projects, db)
                self.assertEqual(rc, 0, "락 점유 중엔 incremental_index 가 skip(0) 해야")
            finally:
                indexer._release_session_lock(held)
            # 락 해제 후엔 정상 인덱싱
            with patch("memory_indexer.embed_text", side_effect=_fake_embed):
                rc2 = indexer.incremental_index(projects, db)
            self.assertEqual(rc2, 1)


class TestSessionBackfillEmbedFail(unittest.TestCase):
    """임베딩 서버 장애 중 backfill 이 세션을 영구 제외(sentinel)하지 않는다."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db = root / "index.db"
        self.projects = root / "projects"
        (self.projects / "slot").mkdir(parents=True)
        self.jsonl = self.projects / "slot" / "sess-content.jsonl"
        self.jsonl.write_text(
            json.dumps({
                "type": "user",
                "message": {"content": "충분히 긴 실제 세션 본문이며 회수 대상이 되는 내용입니다"},
                "timestamp": "2026-01-01T00:00:00",
            }) + "\n"
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _vec_rows(self):
        conn = sqlite3.connect(str(self.db))
        try:
            return conn.execute(
                "SELECT session_id, embedding FROM sessions_vec"
            ).fetchall()
        finally:
            conn.close()

    def test_no_sentinel_on_embed_fail_then_retry_succeeds(self):
        import indexer
        # 1) 서버 다운 상태로 인덱싱 → sessions 행은 생기지만 vec 는 없음
        with patch("memory_indexer.embed_text", side_effect=_embed_down):
            indexer.incremental_index(self.projects, self.db)
        rows = self._vec_rows()
        self.assertEqual(rows, [], "embed 실패 시 incremental 이 vec/sentinel 을 만들면 안 됨")

        # 2) 여전히 서버 다운 상태로 backfill → EmbedUnavailable → sentinel 미기록
        with patch("memory_indexer.embed_text", side_effect=_embed_down):
            counts = indexer.backfill_session_vecs(self.db)
        self.assertEqual(counts["embedded"], 0)
        self.assertEqual(counts["failed"], 1)
        self.assertEqual(
            self._vec_rows(), [],
            "임베딩 서버 장애 시 빈-blob sentinel 을 박으면 영구 제외 — 박으면 안 됨",
        )

        # 3) 서버 복구 후 backfill → 정상 임베딩되어 vec 채워짐 (영구 누락 아님)
        with patch("memory_indexer.embed_text", side_effect=_fake_embed):
            counts2 = indexer.backfill_session_vecs(self.db)
        self.assertEqual(counts2["embedded"], 1)
        rows = self._vec_rows()
        self.assertEqual(len(rows), 1)
        self.assertTrue(len(rows[0][1]) > 0, "복구 후 vec blob 이 비어 있으면 안 됨")


class TestMemoryReindexEmbedFail(unittest.TestCase):
    """메모리 임베딩 실패 시 mtime 미갱신 → 다음 run 재시도 (영구 vec 누락 방지)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.db = root / "index.db"
        self.mem = root / "memory"
        self.mem.mkdir()
        (self.mem / "feedback_alpha.md").write_text(
            "---\nname: alpha\ndescription: 알파 메모리 요지\ntype: feedback\n---\n\n알파 본문 내용입니다."
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _counts(self, table):
        conn = sqlite3.connect(str(self.db))
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            conn.close()

    def test_embed_fail_defers_then_recovers(self):
        from memory_indexer import incremental_index
        # 1) 서버 다운 상태로 인덱싱 → embed 실패 → 이 메모리는 통째로 deferred
        with patch("memory_indexer.embed_text", side_effect=_embed_down):
            r1 = incremental_index([self.mem], db_path=self.db)
        self.assertEqual(r1["updated"], 0, "embed 실패 메모리는 updated 로 카운트되면 안 됨")
        self.assertEqual(self._counts("memories"), 0, "embed 실패 시 mtime 박힌 행이 생기면 안 됨")
        self.assertEqual(self._counts("memories_vec"), 0)

        # 2) 서버 복구 후 인덱싱 → mtime 이 안 박혔으므로 재시도되어 정상 인덱싱
        with patch("memory_indexer.embed_text", side_effect=_fake_embed):
            r2 = incremental_index([self.mem], db_path=self.db)
        self.assertEqual(r2["updated"], 1, "복구 후 재시도되어 인덱싱돼야 함 (영구 누락 아님)")
        self.assertEqual(self._counts("memories"), 1)
        self.assertGreaterEqual(self._counts("memories_vec"), 1)


if __name__ == "__main__":
    unittest.main()
