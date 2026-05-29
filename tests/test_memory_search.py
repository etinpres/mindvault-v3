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

    def test_alias_candidate_survives_score_threshold_gate(self):
        """bug-audit 2026-05-29 (recall-hot-path-5): alias fallback 후보(score=0)가
        score_threshold(0.50) 게이트를 통과해 회수돼야 한다 (sentinel)."""
        from memory_search import recall_memory
        target = str(self.fixture_dir / "feedback_test_mail.md")
        # vec off(None) + fts 무관 질의 → test-mail 은 alias 로만 후보 진입.
        # db_path==DB_PATH 일 때만 alias lookup 하므로 DB_PATH 도 tmp 로 patch.
        with patch("memory_search.embed_text", return_value=None), \
             patch("memory_search.DB_PATH", self.tmp_db), \
             patch("memory_search._alias_boost_paths", return_value={target}):
            results = recall_memory(
                "zzzqqq 외계어 전혀무관 토큰",
                top_k=3,
                score_threshold=0.50,
                db_path=self.tmp_db,
            )
        names = [r["name"] for r in results]
        self.assertIn(
            "test-mail", names,
            "alias 후보가 score_threshold 게이트에서 떨어지면 안 됨 (sentinel 무력)",
        )

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

    def test_base_exception_propagates_through_recall(self):
        """회귀: hook 의 _Timeout(BaseException) 같은 sentinel 이 발생하면
        recall_memory 의 broad `except Exception` 이 swallow 하지 않고 호출자까지
        그대로 propagate 되어야 한다 (FATAL 로깅 + 빈 [] 반환 X).

        이전엔 _Timeout(Exception) 이라 swallow → "recall FATAL" 51건 누적.
        BaseException 으로 바뀐 뒤 stack 을 그대로 unwind 한다.
        """
        from memory_search import recall_memory

        class _SimulatedHookTimeout(BaseException):
            pass

        def _raise_timeout(_text):
            raise _SimulatedHookTimeout()

        with patch("memory_search.embed_text", side_effect=_raise_timeout):
            with self.assertRaises(_SimulatedHookTimeout):
                recall_memory(
                    "임의 query",
                    top_k=3,
                    score_threshold=0.0,
                    db_path=self.tmp_db,
                )

    def test_regular_exception_still_swallowed(self):
        """회귀: 일반 Exception 은 여전히 broad catch → 빈 [] 반환 + FATAL 로깅.
        (BaseException 분기는 timeout sentinel 전용. 평범한 버그는 hook 을
        살리기 위해 swallow 가 정상.)
        """
        from memory_search import recall_memory

        def _raise_runtime(_text):
            raise RuntimeError("simulated upstream failure")

        with patch("memory_search.embed_text", side_effect=_raise_runtime):
            results = recall_memory(
                "임의 query",
                top_k=3,
                score_threshold=0.0,
                db_path=self.tmp_db,
            )
        self.assertEqual(results, [])


class TestFTSEscape(unittest.TestCase):
    """post-ship: _fts_escape는 FTS5 special token을 절대 흘려보내면 안 됨.
    debug.log 실측 회귀:
      - 'next-33 진행' → 'no such column: 33'
      - '~/bin/scan' → 'syntax error near "~"'
      - '이거 뭐였지?' → 'syntax error near "?"'
      - '.md 출력' → 'syntax error near "."'
    """

    def _exec_fts(self, query: str) -> None:
        """빌드된 FTS5 식이 실제로 파싱 가능한지 inmemory DB 로 검증."""
        from memory_search import _fts_escape
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE VIRTUAL TABLE t USING fts5(body, tokenize='unicode61')"
        )
        conn.execute("INSERT INTO t(body) VALUES ('warmup')")
        fts_q = _fts_escape(query)
        # 던져서 syntax error 나면 곧장 fail
        conn.execute("SELECT rowid FROM t WHERE t MATCH ?", (fts_q,)).fetchall()
        conn.close()

    def test_strips_special_punctuation(self):
        for q in [
            "next-33 진행",
            "~/bin/scan 동작 안 함",
            "이거 뭐였지?",
            ".md 출력",
            "8개 후보",
            "Phase.1 결과",
            "description: 사용자",
            "path/to/file.md",
            "what's this",
        ]:
            with self.subTest(query=q):
                self._exec_fts(q)  # 파싱 안 되면 sqlite3.DatabaseError

    def test_pure_digit_tokens_dropped(self):
        """단독 숫자(33, 8)는 FTS5에서 column 참조로 해석 — 반드시 제외."""
        from memory_search import _fts_escape
        out = _fts_escape("33 8 next 진행")
        self.assertNotIn("33*", out)
        self.assertNotIn("8*", out)
        self.assertIn("next*", out)
        self.assertIn("진행*", out)

    def test_only_specials_fallback(self):
        """전부 special 문자뿐이어도 죽지 말고 빈 매치로 fallback."""
        self._exec_fts("???")
        self._exec_fts("...")
        self._exec_fts("~")


class TestProceduralTypeGate(unittest.TestCase):
    """Sprint NEXT-4 — procedural path 는 raw_cosine 게이트가 +0.05 엄격."""

    def test_is_procedural_path(self):
        from memory_search import _is_procedural_path
        self.assertTrue(_is_procedural_path("/x/_procedural/y.md"))
        self.assertTrue(
            _is_procedural_path(
                "/Users/me/.claude/projects/-Users-me-my-folder/memory/_procedural/launchctl.md"
            )
        )
        self.assertFalse(_is_procedural_path("/x/memory/topic.md"))
        self.assertFalse(_is_procedural_path(""))

    def test_gate_for_path_procedural_bonus(self):
        from memory_search import _gate_for_path, PROCEDURAL_GATE_BONUS
        # default
        self.assertAlmostEqual(
            _gate_for_path("/x/_procedural/y.md", 0.40),
            0.40 + PROCEDURAL_GATE_BONUS,
        )
        # hinted
        self.assertAlmostEqual(
            _gate_for_path("/x/_procedural/y.md", 0.32),
            0.32 + PROCEDURAL_GATE_BONUS,
        )
        # non-procedural unchanged
        self.assertAlmostEqual(
            _gate_for_path("/x/memory/y.md", 0.40), 0.40
        )

    def test_gate_disabled_when_min_zero(self):
        from memory_search import _gate_for_path
        self.assertEqual(_gate_for_path("/x/_procedural/y.md", 0.0), 0.0)
        self.assertEqual(_gate_for_path("/x/memory/y.md", 0.0), 0.0)


class TestVecTopKInvalidRowRegression(unittest.TestCase):
    """NEXT-36 (2026-05-26) — invalid embedding row 가 섞여도 mat ↔ meta 인덱스 정합.

    이전: mat[i] (row 인덱스) 와 meta(valid 만 append) 인덱스 미스매치 →
    - results IndexError 또는 잘못된 path 반환
    - raw_map 의 cosine 값이 잘못된 path 에 매핑 (cross-contamination)

    fix: valid 카운터로 mat[valid] = arr; mat = mat[:valid]. search.py:vec_candidates
    와 동일 패턴.
    """

    class _MockRow:
        def __init__(self, path, kind, embedding):
            self._d = {"path": path, "kind": kind, "embedding": embedding}
        def __getitem__(self, k):
            return self._d[k]

    class _MockConn:
        def __init__(self, rows):
            self._rows = rows
        def execute(self, _q):
            rows = self._rows
            class _C:
                def __iter__(self):
                    return iter(rows)
            return _C()

    def _make_query_and_vecs(self, seed=42):
        import numpy as np
        rng = np.random.RandomState(seed)
        v0 = rng.rand(1024).astype(np.float32)
        v2 = rng.rand(1024).astype(np.float32)
        v3 = rng.rand(1024).astype(np.float32)
        return v0, v2, v3

    def test_empty_embedding_row_does_not_break_alignment(self):
        from memory_search import _vec_top_k
        v0, v2, v3 = self._make_query_and_vecs()
        rows = [
            self._MockRow("/p0", "body", v0.tobytes()),
            self._MockRow("/p1_empty", "body", b""),  # invalid sentinel
            self._MockRow("/p2", "body", v2.tobytes()),
            self._MockRow("/p3", "body", v3.tobytes()),
        ]
        conn = self._MockConn(rows)
        results, raw_map = _vec_top_k(conn, v3.tolist(), limit=4)
        # 정합 검증: top-1 은 query 와 동일한 v3 → /p3 path.
        self.assertEqual(results[0][0], "/p3")
        self.assertAlmostEqual(raw_map["/p3"], 1.0, places=4)
        # invalid row 는 raw_map 에서 누락되어야 한다.
        self.assertNotIn("/p1_empty", raw_map)
        # path 와 cosine 매핑이 cross-contamination 없이 정확해야.
        self.assertEqual({"/p0", "/p2", "/p3"}, set(raw_map.keys()))
        self.assertEqual({r[0] for r in results}, {"/p0", "/p2", "/p3"})

    def test_bad_dim_embedding_row_does_not_break_alignment(self):
        import numpy as np
        from memory_search import _vec_top_k
        v0, v2, _ = self._make_query_and_vecs()
        rows = [
            self._MockRow("/p0", "body", v0.tobytes()),
            self._MockRow("/p1_baddim", "body", np.zeros(512, dtype=np.float32).tobytes()),
            self._MockRow("/p2", "body", v2.tobytes()),
        ]
        conn = self._MockConn(rows)
        results, raw_map = _vec_top_k(conn, v0.tolist(), limit=3)
        self.assertEqual(results[0][0], "/p0")
        self.assertNotIn("/p1_baddim", raw_map)
        self.assertEqual({"/p0", "/p2"}, set(raw_map.keys()))

    def test_all_invalid_rows_returns_empty(self):
        from memory_search import _vec_top_k
        v0, _, _ = self._make_query_and_vecs()
        rows = [
            self._MockRow("/p1_empty", "body", b""),
        ]
        conn = self._MockConn(rows)
        results, raw_map = _vec_top_k(conn, v0.tolist(), limit=3)
        self.assertEqual(results, [])
        self.assertEqual(raw_map, {})

    def test_all_valid_rows_backwards_compat(self):
        from memory_search import _vec_top_k
        v0, v2, v3 = self._make_query_and_vecs()
        rows = [
            self._MockRow("/p0", "body", v0.tobytes()),
            self._MockRow("/p2", "body", v2.tobytes()),
            self._MockRow("/p3", "body", v3.tobytes()),
        ]
        conn = self._MockConn(rows)
        results, raw_map = _vec_top_k(conn, v0.tolist(), limit=3)
        self.assertEqual(results[0][0], "/p0")
        self.assertEqual(len(results), 3)
        self.assertEqual(len(raw_map), 3)


class TestFtsEscapeParity(unittest.TestCase):
    """NEXT-36 (2026-05-26) — search.fts_escape 와 memory_search._fts_escape 의
    동등성 회귀 가드. 두 모듈이 동일 토큰 정책을 유지해야 하는데 한 쪽만 수정해
    skew 일으키면 sessions/memory 검색 결과가 달라진다. DRY 위반은 비용 > 효용이라
    유지하되 동등성은 회귀로 묶는다.
    """

    def test_fts_escape_outputs_match(self):
        from search import fts_escape as session_escape
        from memory_search import _fts_escape as memory_escape
        samples = [
            "MindVault recall",
            "fix-the-fix 패턴",
            "1234",
            "?!@#",
            "abc",
            "a",
            "한국어 검색",
            "session_id",
            "",
            "   ",
            "v3.2.9",
            "/path/to/file.md",
        ]
        for q in samples:
            with self.subTest(query=q):
                self.assertEqual(
                    session_escape(q),
                    memory_escape(q),
                    f"fts_escape skew for {q!r}",
                )


class TestResolveWikilinkDeterminism(unittest.TestCase):
    """NEXT-36 (2026-05-26) — 다중 후보 시 ORDER BY path 안정성."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_db = Path(self.tmp_dir.name) / "test.db"
        from indexer import open_db
        conn = open_db(self.tmp_db)
        # 같은 basename 의 두 후보 (다른 경로). frontmatter name 은 둘 다 빈
        # 문자열로 두어 first-clause (name=?) 가 not-found 가 되게 한다.
        conn.execute(
            "INSERT INTO memories(path, name, description, mtime_ns, indexed_at) "
            "VALUES (?, '', 'desc-z', 0, '2026-05-26T00:00:00Z')",
            ("/z/y/foo_bar.md",),
        )
        conn.execute(
            "INSERT INTO memories(path, name, description, mtime_ns, indexed_at) "
            "VALUES (?, '', 'desc-a', 0, '2026-05-26T00:00:00Z')",
            ("/a/b/foo_bar.md",),
        )
        conn.commit()
        self.conn = conn

    def tearDown(self):
        self.conn.close()
        self.tmp_dir.cleanup()

    def test_multiple_candidates_picks_lex_smallest_path(self):
        from memory_search import _resolve_wikilink
        # Insert 순서는 /z 가 먼저였지만 ORDER BY path → /a 가 먼저 와야 한다.
        out = _resolve_wikilink(self.conn, "foo-bar")
        self.assertIsNotNone(out)
        self.assertEqual(out["path"], "/a/b/foo_bar.md")
        self.assertEqual(out["description"], "desc-a")

    def test_resolve_stable_across_repeated_calls(self):
        from memory_search import _resolve_wikilink
        outs = {_resolve_wikilink(self.conn, "foo-bar")["path"] for _ in range(20)}
        self.assertEqual(outs, {"/a/b/foo_bar.md"})


if __name__ == "__main__":
    unittest.main()
