"""dedup_cli — name-dup 탐지·merge·stem-collision rename 검증."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))


def _write_memory(d: Path, stem: str, name: str, body: str) -> Path:
    p = d / f"{stem}.md"
    p.write_text(
        f"---\nname: {name}\ndescription: x\n---\n{body}",
        encoding="utf-8",
    )
    return p


class TestScan(unittest.TestCase):
    def test_no_dups(self):
        from dedup_cli import _scan
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write_memory(d, "a", "name-a", "body a")
            _write_memory(d, "b", "name-b", "body b")
            out = _scan(memory_dirs=[d])
        self.assertEqual(out["name_dups"], [])
        self.assertEqual(out["stem_collisions"], [])
        self.assertEqual(len(out["files"]), 2)

    def test_name_dup_detected(self):
        from dedup_cli import _scan
        with tempfile.TemporaryDirectory() as tmp:
            d1 = Path(tmp) / "r1"
            d2 = Path(tmp) / "r2"
            d1.mkdir(); d2.mkdir()
            _write_memory(d1, "topic1", "Same Name", "body1")
            _write_memory(d2, "topic2", "same name", "body2")  # case-insensitive
            out = _scan(memory_dirs=[d1, d2])
        self.assertEqual(len(out["name_dups"]), 1)
        self.assertEqual(out["name_dups"][0]["key"], "same name")
        self.assertEqual(len(out["name_dups"][0]["files"]), 2)
        # name 동일이면 stem 달라도 stem-collision 에 안 들어감
        self.assertEqual(out["stem_collisions"], [])

    def test_stem_collision_only(self):
        """같은 stem, 다른 name → stem-collision 으로 분류, name-dup 아님."""
        from dedup_cli import _scan
        with tempfile.TemporaryDirectory() as tmp:
            d1 = Path(tmp) / "r1"
            d2 = Path(tmp) / "r2"
            d1.mkdir(); d2.mkdir()
            _write_memory(d1, "project_x", "Project X v1", "body1")
            _write_memory(d2, "project_x", "Project X v2 운영", "body2")
            out = _scan(memory_dirs=[d1, d2])
        self.assertEqual(len(out["stem_collisions"]), 1)
        self.assertEqual(out["stem_collisions"][0]["key"], "project_x")
        # name 다르므로 name-dup 아님
        self.assertEqual(out["name_dups"], [])

    def test_freshness_sort(self):
        """mtime 최신 · size 큰 쪽이 group[0] (canonical 후보)."""
        from dedup_cli import _scan
        import os, time as _t
        with tempfile.TemporaryDirectory() as tmp:
            d1 = Path(tmp) / "r1"; d1.mkdir()
            d2 = Path(tmp) / "r2"; d2.mkdir()
            old = _write_memory(d1, "t", "Same", "x")
            new = _write_memory(d2, "u", "same", "more body" * 20)
            # 강제로 mtime 차이 만들기
            os.utime(old, (1_700_000_000, 1_700_000_000))
            os.utime(new, (1_800_000_000, 1_800_000_000))
            out = _scan(memory_dirs=[d1, d2])
        files = out["name_dups"][0]["files"]
        self.assertEqual(files[0]["path"], str(new))  # 최신 우선


class TestCmdList(unittest.TestCase):
    def test_list_outputs_json(self):
        from dedup_cli import cmd_list
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            _write_memory(d, "a", "name-a", "body")
            buf = io.StringIO()
            with patch("dedup_cli.DEFAULT_MEMORY_DIRS", [d]), \
                 patch("dedup_cli._extra_memory_dirs", return_value=[]), \
                 patch("sys.stdout", buf):
                cmd_list()
            out = json.loads(buf.getvalue())
            self.assertEqual(out["total_indexed"], 1)
            self.assertEqual(out["name_dup_groups"], 0)


class TestCmdMerge(unittest.TestCase):
    def test_merge_dry_run(self):
        from dedup_cli import cmd_merge
        with tempfile.TemporaryDirectory() as tmp:
            d1 = Path(tmp) / "r1"; d1.mkdir()
            d2 = Path(tmp) / "r2"; d2.mkdir()
            old = _write_memory(d1, "old", "same topic", "v1 본문")
            new = _write_memory(d2, "new", "same topic", "v2 본문 (정제됨)")
            buf = io.StringIO()
            with patch("dedup_cli.DEFAULT_MEMORY_DIRS", [d1, d2]), \
                 patch("dedup_cli._extra_memory_dirs", return_value=[]), \
                 patch("memory_compiler._call_gemma", return_value="통합 본문"), \
                 patch("sys.stdout", buf):
                rc = cmd_merge("same topic", dry_run=True)
            self.assertEqual(rc, 0)
            out = json.loads(buf.getvalue())
            self.assertTrue(out["dry_run"])
            self.assertIn("compile_log", out)
            # 원본 파일은 그대로
            self.assertTrue(old.is_file())
            self.assertTrue(new.is_file())

    def test_merge_executes(self):
        from dedup_cli import cmd_merge
        with tempfile.TemporaryDirectory() as tmp:
            d1 = Path(tmp) / "r1"; d1.mkdir()
            d2 = Path(tmp) / "r2"; d2.mkdir()
            old = _write_memory(d1, "old", "same topic", "v1 본문")
            new = _write_memory(d2, "new", "same topic", "v2 본문")
            # new 가 더 새것 (mtime 명시)
            import os
            os.utime(old, (1_700_000_000, 1_700_000_000))
            os.utime(new, (1_800_000_000, 1_800_000_000))
            buf = io.StringIO()
            from types import ModuleType
            stub = ModuleType("memory_indexer_stub")
            stub.incremental_index = lambda: {"updated": 0}
            with patch("dedup_cli.DEFAULT_MEMORY_DIRS", [d1, d2]), \
                 patch("dedup_cli._extra_memory_dirs", return_value=[]), \
                 patch("memory_compiler._call_gemma", return_value="v1+v2 통합본"), \
                 patch.dict("sys.modules", {"memory_indexer": _StubIdx()}), \
                 patch("sys.stdout", buf):
                rc = cmd_merge("same topic", dry_run=False)
            self.assertEqual(rc, 0)
            out = json.loads(buf.getvalue())
            self.assertTrue(out["ok"])
            # canonical = new
            self.assertEqual(out["canonical"], str(new))
            # old 삭제됨, .bak 남음
            self.assertFalse(old.is_file())
            self.assertTrue((old.with_suffix(".md.bak")).is_file())
            # new 도 .bak 백업 + 본문 통합 결과로 overwrite
            self.assertTrue((new.with_suffix(".md.bak")).is_file())
            self.assertIn("v1+v2 통합본", new.read_text())

    def test_merge_missing_group(self):
        from dedup_cli import cmd_merge
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            buf = io.StringIO()
            with patch("dedup_cli.DEFAULT_MEMORY_DIRS", [d]), \
                 patch("dedup_cli._extra_memory_dirs", return_value=[]), \
                 patch("sys.stdout", buf):
                rc = cmd_merge("ghost")
            self.assertEqual(rc, 1)
            out = json.loads(buf.getvalue())
            self.assertFalse(out["ok"])


class TestCmdRename(unittest.TestCase):
    def test_rename_success(self):
        from dedup_cli import cmd_rename
        with tempfile.TemporaryDirectory() as tmp:
            src = _write_memory(Path(tmp), "old_stem", "X", "body")
            buf = io.StringIO()
            with patch.dict(
                "sys.modules", {"memory_indexer": _StubIdx()}
            ), patch("sys.stdout", buf):
                rc = cmd_rename(str(src), "new_stem")
            self.assertEqual(rc, 0)
            self.assertFalse(src.is_file())
            self.assertTrue((Path(tmp) / "new_stem.md").is_file())

    def test_rename_target_exists(self):
        from dedup_cli import cmd_rename
        with tempfile.TemporaryDirectory() as tmp:
            src = _write_memory(Path(tmp), "a", "X", "body")
            _write_memory(Path(tmp), "b", "Y", "body")
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                rc = cmd_rename(str(src), "b")
            self.assertEqual(rc, 1)
            out = json.loads(buf.getvalue())
            self.assertFalse(out["ok"])
            self.assertEqual(out["error"], "target exists")

    def test_rename_rejects_path_traversal(self):
        from dedup_cli import cmd_rename
        with tempfile.TemporaryDirectory() as tmp:
            src = _write_memory(Path(tmp), "ok", "X", "body")
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                rc = cmd_rename(str(src), "../evil")
            self.assertEqual(rc, 1)
            out = json.loads(buf.getvalue())
            self.assertFalse(out["ok"])


class _StubIdx:
    DEFAULT_MEMORY_DIRS = []
    @staticmethod
    def _extra_memory_dirs(): return []
    @staticmethod
    def _collect_md_files(dirs):
        return []
    @staticmethod
    def parse_frontmatter(text):
        return {}, text
    @staticmethod
    def incremental_index(*a, **kw):
        return {"updated": 0, "skipped": 0, "removed": 0}


if __name__ == "__main__":
    unittest.main()
