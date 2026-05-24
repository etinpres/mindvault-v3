"""Sprint 4 Task 3 — memory_indexer 단위 테스트."""
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


class TestParseFrontmatter(unittest.TestCase):
    def test_normal_frontmatter(self):
        from memory_indexer import parse_frontmatter
        md = """---
name: test
description: "hello world"
---

body content here"""
        fm, body = parse_frontmatter(md)
        self.assertEqual(fm.get("name"), "test")
        self.assertEqual(fm.get("description"), "hello world")
        self.assertIn("body content", body)
        self.assertFalse(body.lstrip().startswith("---"))

    def test_no_frontmatter(self):
        from memory_indexer import parse_frontmatter
        md = "just plain body no frontmatter"
        fm, body = parse_frontmatter(md)
        self.assertEqual(fm, {})
        self.assertEqual(body.strip(), md.strip())

    def test_description_missing(self):
        from memory_indexer import parse_frontmatter
        md = """---
name: test
---
body"""
        fm, body = parse_frontmatter(md)
        self.assertEqual(fm.get("name"), "test")
        self.assertIsNone(fm.get("description"))

    def test_malformed_yaml_graceful(self):
        from memory_indexer import parse_frontmatter
        md = """---
name: test
description: "unclosed quote
- broken: [
---
body"""
        fm, body = parse_frontmatter(md)
        self.assertEqual(fm, {})


class TestEmbedText(unittest.TestCase):
    """embed_text 의 urlopen 실패·bad shape 처리.

    주의: production embed_cache 에 'hello'(passage) entry 가 이미 있어 cache hit 하면
    mock urlopen 미도달 → 실패. 모든 케이스에서 _embed_cache_get 도 함께 mock 해 cache
    miss 강제. Sprint 11 BUILD-LOG §"미해결" 4번 해소.
    """
    def test_embed_success(self):
        from memory_indexer import embed_text
        with patch("memory_indexer._embed_cache_get", return_value=None), \
             patch("memory_indexer._embed_cache_put"), \
             patch("memory_indexer.urllib.request.urlopen") as mock_open:
            mock_resp = mock_open.return_value.__enter__.return_value
            mock_resp.read.return_value = json.dumps(
                {"vector": [0.1] * 1024}
            ).encode()
            vec = embed_text("hello")
            self.assertEqual(len(vec), 1024)
            self.assertAlmostEqual(vec[0], 0.1, places=5)

    def test_embed_timeout_returns_none(self):
        from memory_indexer import embed_text
        with patch("memory_indexer._embed_cache_get", return_value=None), \
             patch("memory_indexer.urllib.request.urlopen") as mock_open:
            mock_open.side_effect = TimeoutError("timeout")
            self.assertIsNone(embed_text("hello"))

    def test_embed_connection_refused_returns_none(self):
        from memory_indexer import embed_text
        with patch("memory_indexer._embed_cache_get", return_value=None), \
             patch("memory_indexer.urllib.request.urlopen") as mock_open:
            mock_open.side_effect = urllib.error.URLError("refused")
            self.assertIsNone(embed_text("hello"))

    def test_embed_empty_input_returns_none(self):
        from memory_indexer import embed_text
        self.assertIsNone(embed_text(""))
        self.assertIsNone(embed_text("   "))

    def test_embed_bad_shape_returns_none(self):
        from memory_indexer import embed_text
        with patch("memory_indexer._embed_cache_get", return_value=None), \
             patch("memory_indexer.urllib.request.urlopen") as mock_open:
            mock_resp = mock_open.return_value.__enter__.return_value
            mock_resp.read.return_value = json.dumps(
                {"vector": [0.1] * 512}
            ).encode()
            self.assertIsNone(embed_text("hello"))


def _fake_embed(_text):
    return [0.5] * 1024


class TestIncrementalIndex(unittest.TestCase):
    """fixture는 매번 임시 디렉토리로 격리 복사해 안전하게 수정 가능."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.tmp_db = Path(self.tmp_dir.name) / "test.db"
        src_fixture = Path(__file__).parent / "fixtures" / "memory"
        self.fixture_dir = Path(self.tmp_dir.name) / "memory"
        shutil.copytree(src_fixture, self.fixture_dir)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_initial_index_inserts_rows(self):
        from memory_indexer import incremental_index
        with patch("memory_indexer.embed_text", side_effect=_fake_embed):
            result = incremental_index([self.fixture_dir], db_path=self.tmp_db)
        self.assertEqual(result["updated"], 3)
        self.assertEqual(result["skipped"], 0)
        self.assertEqual(result["removed"], 0)

    def test_lock_stays_in_db_dir_not_production(self):
        """post-ship 회귀 — incremental_index(db_path=tmp) 가 production
        ~/.claude/mindvault-v3/memory-indexer.lock 을 생성하면 안 됨."""
        from memory_indexer import incremental_index, LOCK_PATH
        prod_existed_before = LOCK_PATH.exists()
        prod_mtime_before = LOCK_PATH.stat().st_mtime if prod_existed_before else None
        with patch("memory_indexer.embed_text", side_effect=_fake_embed):
            incremental_index([self.fixture_dir], db_path=self.tmp_db)
        # tmp 디렉토리에 lock 이 생성됐는지
        tmp_lock = self.tmp_db.parent / "memory-indexer.lock"
        self.assertTrue(
            tmp_lock.exists(),
            f"tmp lock 미생성: {tmp_lock}",
        )
        # production lock 이 새로 만들어지거나 mtime 갱신되지 않았어야 함
        if prod_existed_before:
            self.assertEqual(
                LOCK_PATH.stat().st_mtime, prod_mtime_before,
                "production lock mtime 이 갱신됨 — leak",
            )
        else:
            self.assertFalse(
                LOCK_PATH.exists(),
                f"production lock 새로 생성됨: {LOCK_PATH}",
            )

        conn = sqlite3.connect(str(self.tmp_db))
        count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        self.assertEqual(count, 3)
        # description 있는 것 두 개, body 세 개 → vec row 5개
        vec_count = conn.execute("SELECT COUNT(*) FROM memories_vec").fetchone()[0]
        self.assertGreaterEqual(vec_count, 3)  # 최소 body 3
        conn.close()

    def test_second_run_skips_unchanged(self):
        from memory_indexer import incremental_index
        with patch("memory_indexer.embed_text", side_effect=_fake_embed):
            incremental_index([self.fixture_dir], db_path=self.tmp_db)
            result2 = incremental_index([self.fixture_dir], db_path=self.tmp_db)
        self.assertEqual(result2["updated"], 0)
        self.assertEqual(result2["skipped"], 3)

    def test_modified_file_reindexed(self):
        from memory_indexer import incremental_index
        with patch("memory_indexer.embed_text", side_effect=_fake_embed):
            incremental_index([self.fixture_dir], db_path=self.tmp_db)
            target = self.fixture_dir / "feedback_test_mail.md"
            target.write_text(target.read_text() + "\n\n[touch 2026-05-22]")
            result2 = incremental_index([self.fixture_dir], db_path=self.tmp_db)
        self.assertEqual(result2["updated"], 1)
        self.assertEqual(result2["skipped"], 2)

    def test_deleted_file_removed(self):
        from memory_indexer import incremental_index
        with patch("memory_indexer.embed_text", side_effect=_fake_embed):
            incremental_index([self.fixture_dir], db_path=self.tmp_db)
            (self.fixture_dir / "feedback_test_mail.md").unlink()
            result2 = incremental_index([self.fixture_dir], db_path=self.tmp_db)
        self.assertEqual(result2["removed"], 1)
        conn = sqlite3.connect(str(self.tmp_db))
        count = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        conn.close()
        self.assertEqual(count, 2)

    def test_staged_dir_excluded(self):
        from memory_indexer import incremental_index
        staged = self.fixture_dir / "_staged"
        staged.mkdir()
        (staged / "should_be_ignored.md").write_text(
            "---\nname: ignore\ndescription: skip me\n---\nshould not index"
        )
        with patch("memory_indexer.embed_text", side_effect=_fake_embed):
            result = incremental_index([self.fixture_dir], db_path=self.tmp_db)
        self.assertEqual(result["updated"], 3)

    def test_procedural_subdir_indexed(self):
        """Sprint 13: _procedural/ 하위 .md 는 root 직속과 함께 인덱싱."""
        from memory_indexer import incremental_index
        proc = self.fixture_dir / "_procedural"
        proc.mkdir()
        (proc / "claude_bg_syntax.md").write_text(
            "---\nname: claude-bg-syntax\ndescription: claude --bg background session\n"
            "type: procedural\n---\n`claude --bg \"prompt\"` 백그라운드 세션 시작."
        )
        with patch("memory_indexer.embed_text", side_effect=_fake_embed):
            result = incremental_index([self.fixture_dir], db_path=self.tmp_db)
        self.assertEqual(result["updated"], 4)  # fixture 3 + procedural 1
        conn = sqlite3.connect(str(self.tmp_db))
        try:
            paths = [r[0] for r in conn.execute("SELECT path FROM memories")]
        finally:
            conn.close()
        self.assertTrue(
            any("_procedural" in p and "claude_bg_syntax" in p for p in paths),
            f"procedural memory not indexed: {paths}",
        )

    def test_procedural_staged_excluded(self):
        """Sprint 13: _procedural/_staged/ 도 _staged part 일치로 제외."""
        from memory_indexer import incremental_index
        proc_staged = self.fixture_dir / "_procedural" / "_staged"
        proc_staged.mkdir(parents=True)
        (proc_staged / "20260523-010101_procedural_test.md").write_text(
            "---\nname: stage-test\ndescription: skip\ntype: procedural\n---\nstaged"
        )
        with patch("memory_indexer.embed_text", side_effect=_fake_embed):
            result = incremental_index([self.fixture_dir], db_path=self.tmp_db)
        self.assertEqual(result["updated"], 3)  # _staged 제외, fixture 3개만


class TestPathSafety(unittest.TestCase):
    def test_symlink_outside_root_rejected(self):
        from memory_indexer import _safe_memory_path
        with tempfile.TemporaryDirectory() as root, \
             tempfile.TemporaryDirectory() as outside:
            outside_file = Path(outside) / "evil.md"
            outside_file.write_text("---\nname: evil\n---\nbad")
            symlink = Path(root) / "trick.md"
            symlink.symlink_to(outside_file)
            self.assertFalse(_safe_memory_path(symlink, [Path(root)]))

    def test_path_inside_root_accepted(self):
        from memory_indexer import _safe_memory_path
        with tempfile.TemporaryDirectory() as root:
            f = Path(root) / "ok.md"
            f.write_text("ok")
            self.assertTrue(_safe_memory_path(f, [Path(root)]))


if __name__ == "__main__":
    unittest.main()
