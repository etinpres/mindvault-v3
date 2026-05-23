"""Sprint 14 — Memory Compiler 단위 테스트.

Gemma 호출은 모두 _call_gemma mock. 실 서버 의존 없음.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))


class TestSlugifyEquivalence(unittest.TestCase):
    """session_memory_end.slugify 와 memory_compiler.slugify 동등성 — 매칭 일관성 필수."""

    def test_slugify_matches_session_end(self):
        import memory_compiler
        import session_memory_end
        cases = [
            "Hello World",
            "한국어 제목",
            "claude --bg syntax",
            "MindVault v3 Sprint 14",
            "  trim  whitespace  ",
            "특수!@#문자  제거",
        ]
        for title in cases:
            self.assertEqual(
                memory_compiler.slugify(title),
                session_memory_end.slugify(title),
                f"slugify mismatch for {title!r}",
            )


class TestDiffSummary(unittest.TestCase):
    def test_diff_summary_handles_empty(self):
        from memory_compiler import diff_summary
        self.assertEqual(diff_summary("", ""), "")

    def test_diff_summary_counts(self):
        from memory_compiler import diff_summary
        s = diff_summary("a\nb\nc", "a\nB\nc\nd")
        # B 한 줄 교체 + d 추가
        self.assertIn("+", s)
        self.assertIn("-", s)
        self.assertIn("자", s)  # length report


class TestUnifiedDiffText(unittest.TestCase):
    def test_unified_diff_basic(self):
        from memory_compiler import unified_diff_text
        out = unified_diff_text("old line", "new line")
        self.assertIn("---", out)
        self.assertIn("+++", out)


class TestFindExistingMemory(unittest.TestCase):
    def _make_memory_dir(self, files: dict[str, str]) -> Path:
        d = Path(tempfile.mkdtemp())
        for name, content in files.items():
            p = d / name
            p.write_text(content, encoding="utf-8")
        return d

    def test_match_by_frontmatter_name(self):
        from memory_compiler import _find_existing_memory
        d = self._make_memory_dir({
            "anything.md": (
                "---\nname: Claude BG Syntax\ndescription: bg\n---\n"
                "claude --bg foo"
            ),
        })
        cand = {"title": "claude bg syntax", "body": "new fact"}
        out = _find_existing_memory(cand, [d])
        self.assertIsNotNone(out)
        self.assertEqual(out["path"].name, "anything.md")

    def test_match_by_slug_fallback(self):
        from memory_compiler import _find_existing_memory
        d = self._make_memory_dir({
            "my_pattern.md": (
                "---\nname: 다른 이름\ndescription: x\n---\nbody"
            ),
        })
        cand = {"title": "my pattern", "body": "fact"}
        out = _find_existing_memory(cand, [d])
        self.assertIsNotNone(out)
        self.assertEqual(out["path"].stem, "my_pattern")

    def test_no_match(self):
        from memory_compiler import _find_existing_memory
        d = self._make_memory_dir({
            "totally_different.md": "---\nname: X\n---\nbody"
        })
        cand = {"title": "unrelated thing", "body": "fact"}
        self.assertIsNone(_find_existing_memory(cand, [d]))

    def test_name_match_beats_slug(self):
        """name exact 매칭 > slug fallback. 같은 dir 에 둘 다 있으면 name 우선."""
        from memory_compiler import _find_existing_memory
        d = self._make_memory_dir({
            "fallback_slug.md": (
                "---\nname: 다른 이름\ndescription: x\n---\nbody1"
            ),
            "other_name.md": (
                "---\nname: fallback slug\ndescription: y\n---\nbody2"
            ),
        })
        cand = {"title": "fallback slug", "body": "fact"}
        out = _find_existing_memory(cand, [d])
        # name 매칭은 other_name.md 의 name 필드
        self.assertEqual(out["path"].stem, "other_name")


class TestEmbeddingFallback(unittest.TestCase):
    """Sprint NEXT-2 — 3순위 embedding 의미 매칭."""

    def _make_memory_dir(self, files: dict[str, str]) -> Path:
        d = Path(tempfile.mkdtemp())
        for name, content in files.items():
            (d / name).write_text(content, encoding="utf-8")
        return d

    def _mock_db_with_rows(self, rows: list[tuple[str, str, bytes]]):
        """conn.execute(...).__iter__ → row dict 시뮬레이션. memories_vec 모킹용."""
        import sqlite3
        # 메모리 sqlite 에 실제 테이블 만들고 rows insert — 가장 robust
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE memories_vec (path TEXT, kind TEXT, embedding BLOB)"
        )
        conn.executemany(
            "INSERT INTO memories_vec(path, kind, embedding) VALUES (?,?,?)",
            rows,
        )
        conn.commit()
        return conn

    def test_embedding_hit_above_threshold(self):
        import memory_compiler
        import memory_indexer
        import indexer
        import numpy as np
        d = self._make_memory_dir({
            "topic_one.md": (
                "---\nname: claude-bg-syntax\ndescription: x\n---\n"
                "claude --bg 명령 사용법"
            ),
        })
        # 같은 방향 벡터 → cosine ≈ 1.0 > threshold
        vec = (np.ones(1024, dtype=np.float32) / np.float32(np.sqrt(1024))).astype(np.float32)
        path_str = str((d / "topic_one.md").resolve())
        rows = [(path_str, "passage", vec.tobytes())]
        conn = self._mock_db_with_rows(rows)
        cand = {
            "title": "백그라운드 세션 시작",
            "body": "claude --bg 으로 백그라운드 실행",
        }
        # 모듈 attribute 호출 → patch.object 로 깨끗하게 격리
        with patch.object(
            memory_indexer, "embed_text", lambda text, kind="passage": vec.tolist()
        ), patch.object(indexer, "open_db", lambda: conn):
            try:
                out = memory_compiler._find_existing_memory(cand, [d])
            finally:
                conn.close()
        self.assertIsNotNone(out)
        self.assertEqual(out["path"].name, "topic_one.md")
        self.assertEqual(out.get("match_kind"), "embedding")
        self.assertGreaterEqual(out["cosine"], memory_compiler.EMBED_MATCH_THRESHOLD)

    def test_embedding_miss_below_threshold(self):
        import memory_compiler
        import memory_indexer
        import indexer
        import numpy as np
        d = self._make_memory_dir({
            "topic_one.md": (
                "---\nname: claude-bg-syntax\ndescription: x\n---\n본문"
            ),
        })
        # 직교 벡터 → cosine = 0 < 0.75
        vec_db = np.zeros(1024, dtype=np.float32); vec_db[0] = 1.0
        vec_q = np.zeros(1024, dtype=np.float32); vec_q[1] = 1.0
        path_str = str((d / "topic_one.md").resolve())
        rows = [(path_str, "passage", vec_db.tobytes())]
        conn = self._mock_db_with_rows(rows)
        cand = {"title": "전혀 다른 주제", "body": "다른 사실"}
        with patch.object(
            memory_indexer, "embed_text", lambda text, kind="passage": vec_q.tolist()
        ), patch.object(indexer, "open_db", lambda: conn):
            try:
                out = memory_compiler._find_existing_memory(cand, [d])
            finally:
                conn.close()
        self.assertIsNone(out)

    def test_embedding_called_only_after_name_slug_fail(self):
        """name 또는 slug 매칭이 있으면 embedding 호출 안 함 (cost 보호)."""
        import memory_compiler
        import memory_indexer
        d = self._make_memory_dir({
            "claude_bg_syntax.md": (
                "---\nname: claude-bg-syntax\ndescription: x\n---\nbody"
            ),
        })
        cand = {"title": "claude-bg-syntax", "body": "fact"}
        call_count = {"n": 0}
        def fake_embed(text, kind="passage"):
            call_count["n"] += 1
            return [0.0] * 1024
        with patch.object(memory_indexer, "embed_text", fake_embed):
            out = memory_compiler._find_existing_memory(cand, [d])
        self.assertIsNotNone(out)
        self.assertEqual(call_count["n"], 0, "name 매칭됐는데 embedding 호출됨")

    def test_embedding_failure_returns_none_gracefully(self):
        """embed_text 가 None 반환 (서버 다운) → 매칭 없음, 예외 안 던짐."""
        import memory_compiler
        import memory_indexer
        d = self._make_memory_dir({})  # 빈 dir — name/slug 매칭 없음
        cand = {"title": "어떤 주제", "body": "어떤 본문"}
        with patch.object(memory_indexer, "embed_text", lambda text, kind="passage": None):
            out = memory_compiler._find_existing_memory(cand, [d])
        self.assertIsNone(out)


class TestCompileCandidates(unittest.TestCase):
    def _setup_dir(self):
        d = Path(tempfile.mkdtemp())
        (d / "existing.md").write_text(
            "---\nname: existing topic\ndescription: x\n---\n"
            "기존 본문. v1.0.0 사용 중.",
            encoding="utf-8",
        )
        return d

    def test_no_candidates(self):
        from memory_compiler import compile_candidates
        self.assertEqual(compile_candidates([], memory_dirs=[]), [])

    def test_new_candidate_passes_through(self):
        from memory_compiler import compile_candidates
        d = self._setup_dir()
        cand = {
            "type": "feedback",
            "title": "totally new",
            "body": "new fact",
            "reason": "r",
            "evidence": "e",
        }
        out = compile_candidates([cand], memory_dirs=[d])
        self.assertEqual(len(out), 1)
        self.assertNotIn("update_of", out[0])
        self.assertEqual(out[0]["body"], "new fact")

    def test_matching_candidate_becomes_update(self):
        import memory_compiler
        d = self._setup_dir()
        cand = {
            "type": "feedback",
            "title": "existing topic",
            "body": "v2.0.0 으로 바뀜",
            "reason": "r",
            "evidence": "e",
        }
        with patch.object(
            memory_compiler,
            "_call_gemma",
            return_value="기존 본문. v2.0.0 사용 중.",
        ):
            out = memory_compiler.compile_candidates([cand], memory_dirs=[d])
        self.assertEqual(len(out), 1)
        self.assertIn("update_of", out[0])
        self.assertTrue(out[0]["update_of"].endswith("existing.md"))
        self.assertIn("v2.0.0", out[0]["body"])
        self.assertIn("diff_summary", out[0])

    def test_gemma_failure_keeps_original(self):
        import memory_compiler
        d = self._setup_dir()
        cand = {
            "type": "feedback",
            "title": "existing topic",
            "body": "new fact",
            "reason": "r",
            "evidence": "e",
        }
        with patch.object(memory_compiler, "_call_gemma", return_value=None):
            out = memory_compiler.compile_candidates([cand], memory_dirs=[d])
        # Gemma 실패 → 원본 유지, update_of 없음
        self.assertNotIn("update_of", out[0])
        self.assertEqual(out[0]["body"], "new fact")

    def test_strips_markdown_fences(self):
        import memory_compiler
        d = self._setup_dir()
        cand = {
            "type": "feedback",
            "title": "existing topic",
            "body": "v3 fact",
            "reason": "r",
            "evidence": "e",
        }
        # Gemma 가 가끔 markdown fence 추가
        with patch.object(
            memory_compiler,
            "_call_gemma",
            return_value="```\nv3 정제 본문\n```",
        ):
            out = memory_compiler.compile_candidates([cand], memory_dirs=[d])
        self.assertNotIn("```", out[0]["body"])
        self.assertIn("v3 정제 본문", out[0]["body"])


class TestAutoCompileEnabled(unittest.TestCase):
    def test_default_off(self):
        import memory_compiler
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("MV2_AUTO_COMPILE", None)
            self.assertFalse(memory_compiler.auto_compile_enabled())

    def test_on_when_one(self):
        import memory_compiler
        with patch.dict("os.environ", {"MV2_AUTO_COMPILE": "1"}):
            self.assertTrue(memory_compiler.auto_compile_enabled())

    def test_off_for_other_values(self):
        import memory_compiler
        for val in ("0", "true", "yes", ""):
            with patch.dict("os.environ", {"MV2_AUTO_COMPILE": val}):
                self.assertFalse(
                    memory_compiler.auto_compile_enabled(),
                    f"unexpected on for {val!r}",
                )


class TestSessionEndIntegration(unittest.TestCase):
    """session_memory_end.write_staged 가 update_of/diff_summary 메타 보존하는지."""

    def test_write_staged_includes_update_meta(self):
        import session_memory_end as sme
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch.object(sme, "MEMORY_DIR", tmp_path), \
                 patch.object(sme, "STAGED_DIR", tmp_path / "_staged"), \
                 patch.object(sme, "PROCEDURAL_DIR", tmp_path / "_procedural"), \
                 patch.object(
                     sme,
                     "PROCEDURAL_STAGED_DIR",
                     tmp_path / "_procedural" / "_staged",
                 ):
                item = {
                    "type": "feedback",
                    "title": "topic A",
                    "body": "compiled body",
                    "reason": "r",
                    "evidence": "e",
                    "update_of": "/some/path/topic_a.md",
                    "diff_summary": "+3 -1 (50자 ← 30자)",
                }
                path = sme.write_staged(item, "abc12345")
                text = path.read_text(encoding="utf-8")
                self.assertIn("update_of: /some/path/topic_a.md", text)
                self.assertIn("diff_summary: +3 -1", text)

    def test_write_staged_without_update_meta_unchanged(self):
        """기존 경로 — update_of 없으면 prior frontmatter 그대로."""
        import session_memory_end as sme
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch.object(sme, "MEMORY_DIR", tmp_path), \
                 patch.object(sme, "STAGED_DIR", tmp_path / "_staged"), \
                 patch.object(sme, "PROCEDURAL_DIR", tmp_path / "_procedural"), \
                 patch.object(
                     sme,
                     "PROCEDURAL_STAGED_DIR",
                     tmp_path / "_procedural" / "_staged",
                 ):
                item = {
                    "type": "feedback",
                    "title": "topic B",
                    "body": "fresh body",
                    "reason": "r",
                    "evidence": "e",
                }
                path = sme.write_staged(item, "abc12345")
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("update_of:", text)
                self.assertNotIn("diff_summary:", text)


class TestReviewCliUpdateFlow(unittest.TestCase):
    """memory_review_cli 의 update flow — diff + approve."""

    def test_diff_for_update_candidate(self):
        import memory_review_cli as mrc
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mem = root / "memory"
            mem.mkdir()
            staged = mem / "_staged"
            staged.mkdir()
            existing = mem / "topic.md"
            existing.write_text(
                "---\nname: topic\n---\nold body line 1\nold body line 2",
                encoding="utf-8",
            )
            staged_file = staged / "20260523-010101_feedback_topic.md"
            staged_file.write_text(
                "---\nname: topic\ndescription: x\ntype: feedback\n"
                f"update_of: {existing}\n"
                "diff_summary: +1 -1\n---\n"
                "new body line 1\nold body line 2",
                encoding="utf-8",
            )
            import io
            buf = io.StringIO()
            with patch.object(mrc, "STAGED_DIR", staged), \
                 patch.object(
                     mrc,
                     "PROCEDURAL_STAGED_DIR",
                     mem / "_procedural" / "_staged",
                 ), \
                 patch.object(
                     mrc,
                     "STAGED_DIRS",
                     (staged, mem / "_procedural" / "_staged"),
                 ), \
                 patch.object(
                     mrc, "_allowed_update_roots", return_value=[mem]
                 ), \
                 patch("sys.stdout", buf):
                rc = mrc.cmd_diff(staged_file.name)
            self.assertEqual(rc, 0)
            out = json.loads(buf.getvalue())
            self.assertTrue(out["ok"])
            self.assertEqual(out["kind"], "update")
            self.assertIn("old body line 1", out["unified_diff"])
            self.assertIn("new body line 1", out["unified_diff"])

    def test_diff_for_new_candidate(self):
        import memory_review_cli as mrc
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staged = root / "_staged"
            staged.mkdir()
            staged_file = staged / "20260523-010101_feedback_new_topic.md"
            staged_file.write_text(
                "---\nname: new topic\ndescription: x\ntype: feedback\n---\n"
                "brand new body",
                encoding="utf-8",
            )
            import io
            buf = io.StringIO()
            with patch.object(mrc, "STAGED_DIR", staged), \
                 patch.object(
                     mrc,
                     "PROCEDURAL_STAGED_DIR",
                     root / "_procedural" / "_staged",
                 ), \
                 patch.object(
                     mrc,
                     "STAGED_DIRS",
                     (staged, root / "_procedural" / "_staged"),
                 ), \
                 patch("sys.stdout", buf):
                rc = mrc.cmd_diff(staged_file.name)
            self.assertEqual(rc, 0)
            out = json.loads(buf.getvalue())
            self.assertEqual(out["kind"], "new")
            self.assertIn("brand new body", out["body"])

    def test_approve_update_writes_backup_and_overwrites(self):
        import memory_review_cli as mrc
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mem = root / "memory"
            mem.mkdir()
            staged = mem / "_staged"
            staged.mkdir()
            existing = mem / "topic.md"
            existing.write_text(
                "---\nname: topic\ndescription: x\ntype: feedback\n---\n"
                "old body",
                encoding="utf-8",
            )
            staged_file = staged / "20260523-010101_feedback_topic.md"
            staged_file.write_text(
                "---\nname: topic\ndescription: x\ntype: feedback\n"
                f"update_of: {existing}\n"
                "diff_summary: +1 -1\n---\n"
                "new compiled body",
                encoding="utf-8",
            )
            import io
            buf = io.StringIO()
            # reindex 는 production DB 건드리지 않도록 mock
            with patch.object(mrc, "STAGED_DIR", staged), \
                 patch.object(
                     mrc,
                     "PROCEDURAL_STAGED_DIR",
                     mem / "_procedural" / "_staged",
                 ), \
                 patch.object(
                     mrc,
                     "STAGED_DIRS",
                     (staged, mem / "_procedural" / "_staged"),
                 ), \
                 patch.object(
                     mrc, "_allowed_update_roots", return_value=[mem]
                 ), \
                 patch.dict("sys.modules", {"memory_indexer": _StubIndexer()}), \
                 patch("sys.stdout", buf):
                rc = mrc.cmd_approve(staged_file.name)
            self.assertEqual(rc, 0)
            out = json.loads(buf.getvalue())
            self.assertTrue(out["ok"], out)
            self.assertEqual(out["kind"], "update")
            # 기존 파일 본문 교체됨
            self.assertIn("new compiled body", existing.read_text())
            # frontmatter name/description/type 보존 (기존 값 우선)
            self.assertIn("name: topic", existing.read_text())
            # backup 파일 존재
            bak = existing.with_suffix(".md.bak")
            self.assertTrue(bak.is_file())
            self.assertIn("old body", bak.read_text())
            # staged 파일 삭제됨
            self.assertFalse(staged_file.exists())


class _StubIndexer:
    """memory_indexer mock — incremental_index 호출 시 noop dict 반환."""
    DEFAULT_MEMORY_DIRS = []

    @staticmethod
    def _extra_memory_dirs():
        return []

    @staticmethod
    def incremental_index(*a, **kw):
        return {"updated": 0, "skipped": 0, "removed": 0}


class TestPrettyDiff(unittest.TestCase):
    """Sprint NEXT-5 — diff CLI --pretty 옵션 ANSI 색상."""

    def test_colorize_diff_plus_minus_hunks(self):
        from memory_review_cli import (
            _colorize_diff,
            ANSI_GREEN,
            ANSI_RED,
            ANSI_MAGENTA,
            ANSI_BOLD_BLUE,
            ANSI_RESET,
        )
        diff = (
            "--- existing\n"
            "+++ compiled\n"
            "@@ -1,2 +1,2 @@\n"
            "-old line\n"
            "+new line\n"
            " context line\n"
        )
        out = _colorize_diff(diff)
        self.assertIn(ANSI_BOLD_BLUE + "--- existing" + ANSI_RESET, out)
        self.assertIn(ANSI_BOLD_BLUE + "+++ compiled" + ANSI_RESET, out)
        self.assertIn(ANSI_MAGENTA + "@@ -1,2 +1,2 @@" + ANSI_RESET, out)
        self.assertIn(ANSI_RED + "-old line" + ANSI_RESET, out)
        self.assertIn(ANSI_GREEN + "+new line" + ANSI_RESET, out)
        # context line 은 무색
        self.assertIn(" context line", out)
        self.assertNotIn(ANSI_GREEN + " context line", out)

    def test_should_use_color_pretty_flag(self):
        from memory_review_cli import _should_use_color
        self.assertTrue(_should_use_color(True))
        self.assertFalse(_should_use_color(False))

    def test_cmd_diff_pretty_for_update(self):
        """pretty=True 일 때 JSON 대신 ANSI 색상 plain text 출력."""
        import memory_review_cli as mrc
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            mem = root / "memory"
            mem.mkdir()
            staged = mem / "_staged"
            staged.mkdir()
            existing = mem / "topic.md"
            existing.write_text(
                "---\nname: topic\n---\nold body",
                encoding="utf-8",
            )
            staged_file = staged / "20260523-010101_feedback_topic.md"
            staged_file.write_text(
                "---\nname: topic\ndescription: x\ntype: feedback\n"
                f"update_of: {existing}\n"
                "diff_summary: +1 -1\n---\n"
                "new body",
                encoding="utf-8",
            )
            import io
            buf = io.StringIO()
            with patch.object(mrc, "STAGED_DIR", staged), \
                 patch.object(
                     mrc, "PROCEDURAL_STAGED_DIR", mem / "_procedural" / "_staged"
                 ), \
                 patch.object(
                     mrc, "STAGED_DIRS", (staged, mem / "_procedural" / "_staged")
                 ), \
                 patch.object(mrc, "_allowed_update_roots", return_value=[mem]), \
                 patch("sys.stdout", buf):
                rc = mrc.cmd_diff(staged_file.name, pretty=True)
            self.assertEqual(rc, 0)
            text = buf.getvalue()
            # JSON 아님
            self.assertFalse(text.lstrip().startswith("{"))
            # 헤더 + ANSI 색상 포함
            self.assertIn("[update]", text)
            self.assertIn("\033[31m-old body", text)
            self.assertIn("\033[32m+new body", text)

    def test_cmd_diff_pretty_for_new_candidate(self):
        import memory_review_cli as mrc
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staged = root / "_staged"
            staged.mkdir()
            staged_file = staged / "20260523-010101_feedback_new_topic.md"
            staged_file.write_text(
                "---\nname: new topic\ndescription: x\ntype: feedback\n---\n"
                "fresh body",
                encoding="utf-8",
            )
            import io
            buf = io.StringIO()
            with patch.object(mrc, "STAGED_DIR", staged), \
                 patch.object(
                     mrc, "PROCEDURAL_STAGED_DIR", root / "_procedural" / "_staged"
                 ), \
                 patch.object(
                     mrc, "STAGED_DIRS", (staged, root / "_procedural" / "_staged")
                 ), \
                 patch("sys.stdout", buf):
                rc = mrc.cmd_diff(staged_file.name, pretty=True)
            self.assertEqual(rc, 0)
            text = buf.getvalue()
            self.assertFalse(text.lstrip().startswith("{"))
            self.assertIn("[new]", text)
            self.assertIn("fresh body", text)


if __name__ == "__main__":
    unittest.main()
