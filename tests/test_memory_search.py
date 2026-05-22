"""Sprint 4 Task 4 — memory_search hybrid RRF 단위 테스트."""
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


class TestRRFFusion(unittest.TestCase):
    def test_rrf_single_source(self):
        from memory_search import rrf_combine
        vec_results = [("a.md", 1, "body"), ("b.md", 2, "body"), ("c.md", 3, "body")]
        fts_results: list = []
        combined = rrf_combine(vec_results, fts_results, k=60)
        self.assertAlmostEqual(combined["a.md"]["score"], 1 / 61, places=5)
        self.assertEqual(combined["a.md"]["source"], ["vec"])

    def test_rrf_both_sources_aggregate(self):
        from memory_search import rrf_combine
        vec_results = [("a.md", 1, "body")]
        fts_results = [("a.md", 1, "")]
        combined = rrf_combine(vec_results, fts_results, k=60)
        self.assertAlmostEqual(combined["a.md"]["score"], 2 / 61, places=5)
        self.assertEqual(set(combined["a.md"]["source"]), {"vec", "fts"})

    def test_rrf_description_weight(self):
        from memory_search import rrf_combine
        vec_results = [("a.md", 1, "description")]
        fts_results: list = []
        combined = rrf_combine(vec_results, fts_results, k=60)
        self.assertAlmostEqual(combined["a.md"]["score"], 1.5 / 61, places=5)

    def test_rrf_empty_inputs(self):
        from memory_search import rrf_combine
        self.assertEqual(rrf_combine([], [], k=60), {})


class TestNormalization(unittest.TestCase):
    def test_normalize_minmax(self):
        from memory_search import normalize_scores
        combined = {
            "a.md": {"score": 0.05, "source": ["vec"]},
            "b.md": {"score": 0.02, "source": ["fts"]},
            "c.md": {"score": 0.01, "source": ["vec"]},
        }
        normalize_scores(combined)
        self.assertEqual(combined["a.md"]["score"], 1.0)
        self.assertEqual(combined["c.md"]["score"], 0.0)
        self.assertGreater(combined["b.md"]["score"], 0.0)
        self.assertLess(combined["b.md"]["score"], 1.0)

    def test_normalize_single_entry(self):
        from memory_search import normalize_scores
        combined = {"a.md": {"score": 0.05, "source": ["vec"]}}
        normalize_scores(combined)
        self.assertEqual(combined["a.md"]["score"], 1.0)

    def test_normalize_empty(self):
        from memory_search import normalize_scores
        combined: dict = {}
        normalize_scores(combined)
        self.assertEqual(combined, {})


def _fake_embed(_text):
    """1024차원, 모두 0.5인 unit vector."""
    return [0.5] * 1024


class TestRecallMemory(unittest.TestCase):
    """실 DB + memories_vec(BLOB) 인덱싱 후 검색 검증."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_db = Path(self.tmp_dir.name) / "test.db"
        src_fixture = Path(__file__).parent / "fixtures" / "memory"
        self.fixture_dir = Path(self.tmp_dir.name) / "memory"
        shutil.copytree(src_fixture, self.fixture_dir)
        # 인덱싱
        from memory_indexer import incremental_index
        with patch("memory_indexer.embed_text", side_effect=_fake_embed):
            incremental_index([self.fixture_dir], db_path=self.tmp_db)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_recall_empty_when_no_hit(self):
        from memory_search import recall_memory
        with patch("memory_search.embed_text", return_value=None):
            results = recall_memory(
                "완전히 매칭 안 되는 외계어 zzzqqq",
                top_k=3,
                score_threshold=0.99,
                db_path=self.tmp_db,
            )
        self.assertEqual(results, [])

    def test_recall_fts_hit(self):
        from memory_search import recall_memory
        # vec는 mock None → FTS5만 작동
        with patch("memory_search.embed_text", return_value=None):
            results = recall_memory(
                "메일",
                top_k=3,
                score_threshold=0.0,
                db_path=self.tmp_db,
            )
        self.assertGreaterEqual(len(results), 1)
        names = [r["name"] for r in results]
        self.assertIn("test-mail", names)

    def test_recall_returns_full_schema(self):
        from memory_search import recall_memory
        with patch("memory_search.embed_text", return_value=None):
            results = recall_memory(
                "메일",
                top_k=3,
                score_threshold=0.0,
                db_path=self.tmp_db,
            )
        self.assertTrue(results)
        r = results[0]
        for key in ("path", "name", "description", "snippet", "score", "source"):
            self.assertIn(key, r)
        self.assertIsInstance(r["score"], float)
        self.assertIsInstance(r["source"], list)

    def test_recall_vec_only_when_no_fts(self):
        """FTS에 매칭 안 되는 쿼리지만 vec embedding이 들어오면 vec source로 hit."""
        from memory_search import recall_memory
        # vec은 모든 row에 대해 동일하게 거리 0 (fake_embed=[0.5]*1024 동일)
        with patch("memory_search.embed_text", return_value=[0.5] * 1024):
            results = recall_memory(
                "완전히 매칭 안 되는 zzzqqq",
                top_k=5,
                score_threshold=0.0,
                db_path=self.tmp_db,
            )
        # vec만으로도 결과 반환
        self.assertGreater(len(results), 0)
        # source에 vec 포함
        all_sources = {s for r in results for s in r["source"]}
        self.assertIn("vec", all_sources)


if __name__ == "__main__":
    unittest.main()
