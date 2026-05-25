"""Sprint 16 — Multi-source CLI 단위 테스트."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))


class TestSourcesCli(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self.tmp.name) / "sources.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_missing_returns_empty(self):
        from sources_cli import load_sources
        self.assertEqual(load_sources(self.cfg), [])

    def test_add_and_list(self):
        from sources_cli import cmd_add, load_sources
        target_dir = Path(self.tmp.name) / "src1"
        target_dir.mkdir()
        with patch("sources_cli.CONFIG_PATH", self.cfg):
            rc = cmd_add(str(target_dir))
            self.assertEqual(rc, 0)
        self.assertIn(str(target_dir.absolute()), load_sources(self.cfg))

    def test_add_dedups(self):
        from sources_cli import cmd_add, load_sources
        target_dir = Path(self.tmp.name) / "src1"
        target_dir.mkdir()
        with patch("sources_cli.CONFIG_PATH", self.cfg):
            cmd_add(str(target_dir))
            cmd_add(str(target_dir))
        self.assertEqual(len(load_sources(self.cfg)), 1)

    def test_add_rejects_non_dir(self):
        from sources_cli import cmd_add, load_sources
        with patch("sources_cli.CONFIG_PATH", self.cfg):
            rc = cmd_add(str(Path(self.tmp.name) / "no_such"))
        self.assertEqual(rc, 1)
        self.assertEqual(load_sources(self.cfg), [])

    def test_remove(self):
        from sources_cli import cmd_add, cmd_remove, load_sources
        target_dir = Path(self.tmp.name) / "src1"
        target_dir.mkdir()
        with patch("sources_cli.CONFIG_PATH", self.cfg):
            cmd_add(str(target_dir))
            self.assertEqual(len(load_sources(self.cfg)), 1)
            rc = cmd_remove(str(target_dir))
        self.assertEqual(rc, 0)
        self.assertEqual(load_sources(self.cfg), [])

    def test_remove_idempotent(self):
        from sources_cli import cmd_remove
        with patch("sources_cli.CONFIG_PATH", self.cfg):
            rc = cmd_remove("/no/such")
        self.assertEqual(rc, 0)  # 존재 안 해도 OK 반환

    def test_save_sources_atomic(self):
        """v3.2.6 Round 2 LR2: save_sources 가 tmp + os.replace — partial JSON
        손상으로 indexer 가 색인 path 깨지지 않게."""
        from sources_cli import save_sources
        save_sources(["/some/dir"], config_path=self.cfg)
        leftover = list(self.cfg.parent.glob("*.tmp"))
        self.assertEqual(leftover, [])
        import inspect, sources_cli
        src = inspect.getsource(sources_cli.save_sources)
        self.assertIn("os.replace", src)
        self.assertIn(".tmp", src)


class TestIndexerExtraDirsUnion(unittest.TestCase):
    """memory_indexer._extra_memory_dirs() 가 env + config 합치는지."""

    def test_union_with_dedup(self):
        import memory_indexer
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "sources.json"
            cfg.write_text(
                json.dumps({"sources": ["/path/from/config", "/shared"]}),
                encoding="utf-8",
            )
            with patch.object(memory_indexer, "SOURCES_CONFIG", cfg), \
                 patch.dict(
                     "os.environ",
                     {"MV3_EXTRA_MEMORY_DIRS": "/path/from/env:/shared"},
                 ):
                dirs = memory_indexer._extra_memory_dirs()
            paths = [str(p) for p in dirs]
            self.assertIn("/path/from/env", paths)
            self.assertIn("/path/from/config", paths)
            # /shared 가 env+config 둘 다 있어도 1번만
            self.assertEqual(paths.count("/shared"), 1)
            # env 가 앞 (우선 추가)
            self.assertLess(paths.index("/shared"), paths.index("/path/from/config"))

    def test_missing_config_only_env(self):
        import memory_indexer
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "no_such.json"
            with patch.object(memory_indexer, "SOURCES_CONFIG", cfg), \
                 patch.dict(
                     "os.environ",
                     {"MV3_EXTRA_MEMORY_DIRS": "/env_only"},
                 ):
                dirs = memory_indexer._extra_memory_dirs()
            paths = [str(p) for p in dirs]
            self.assertEqual(paths, ["/env_only"])

    def test_both_empty_returns_empty(self):
        import memory_indexer
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "no_such.json"
            with patch.object(memory_indexer, "SOURCES_CONFIG", cfg), \
                 patch.dict("os.environ", {}, clear=False):
                import os
                os.environ.pop("MV3_EXTRA_MEMORY_DIRS", None)
                dirs = memory_indexer._extra_memory_dirs()
            self.assertEqual(dirs, [])
