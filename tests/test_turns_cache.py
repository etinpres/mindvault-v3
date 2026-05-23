"""Sprint NEXT-7 — turns_cache 단위 테스트."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))


def _make_jsonl(path: Path, turns: list[dict]) -> None:
    """간단 jsonl 생성. turns: [{type, ts_offset, text/tool}]"""
    lines = []
    base = time.time() - 3600
    for i, t in enumerate(turns):
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(base + i))
        if t["type"] == "user":
            msg = {"content": t["text"]}
        else:
            blocks = [{"type": "text", "text": t.get("text", "")}]
            if t.get("tool"):
                blocks.append({"type": "tool_use", "name": t["tool"]})
            msg = {"content": blocks}
        lines.append(
            json.dumps(
                {
                    "type": t["type"],
                    "timestamp": ts,
                    "message": msg,
                }
            )
        )
    path.write_text("\n".join(lines), encoding="utf-8")


class TestTurnsCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "projects"
        self.root.mkdir()
        self.proj = self.root / "p1"
        self.proj.mkdir()
        self.jsonl = self.proj / "sess1.jsonl"
        _make_jsonl(self.jsonl, [
            {"type": "user", "text": "hello"},
            {"type": "assistant", "text": "world", "tool": "Bash"},
            {"type": "user", "text": "more"},
        ])
        self.db = Path(self.tmp.name) / "cache.db"

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_refresh_indexes_all(self):
        import turns_cache as tc
        stat = tc.refresh_cache(projects_root=self.root, db_path=self.db)
        self.assertEqual(stat["scanned"], 1)
        self.assertEqual(stat["reindexed"], 1)
        self.assertEqual(stat["skipped"], 0)

    def test_second_refresh_skips_unchanged(self):
        import turns_cache as tc
        tc.refresh_cache(projects_root=self.root, db_path=self.db)
        stat = tc.refresh_cache(projects_root=self.root, db_path=self.db)
        self.assertEqual(stat["reindexed"], 0)
        self.assertEqual(stat["skipped"], 1)

    def test_mtime_change_triggers_reindex(self):
        import turns_cache as tc
        tc.refresh_cache(projects_root=self.root, db_path=self.db)
        # touch — ns 단위 future
        future = int(time.time() + 60) * 10**9
        os.utime(self.jsonl, ns=(future, future))
        stat = tc.refresh_cache(projects_root=self.root, db_path=self.db)
        self.assertEqual(stat["reindexed"], 1)

    def test_get_turns_returns_indexed(self):
        import turns_cache as tc
        tc.refresh_cache(projects_root=self.root, db_path=self.db)
        turns = tc.get_turns_since(
            0,
            projects_root=self.root,
            db_path=self.db,
            auto_refresh=False,
        )
        roles = [t["role"] for t in turns]
        self.assertEqual(roles, ["user", "assistant", "user"])
        # assistant turn 의 tool_uses 보존
        assistant_tools = [
            t["tool_uses"] for t in turns if t["role"] == "assistant"
        ][0]
        self.assertIn("Bash", assistant_tools)

    def test_get_turns_since_filter(self):
        """since_unix 이후 turn 만."""
        import turns_cache as tc
        tc.refresh_cache(projects_root=self.root, db_path=self.db)
        # 미래 since → 0건
        future_since = time.time() + 86400
        turns = tc.get_turns_since(
            future_since,
            projects_root=self.root,
            db_path=self.db,
            auto_refresh=False,
        )
        self.assertEqual(len(turns), 0)

    def test_full_rebuild_clears_old(self):
        import turns_cache as tc
        tc.refresh_cache(projects_root=self.root, db_path=self.db)
        # jsonl 변경 (line 추가)
        _make_jsonl(self.jsonl, [
            {"type": "user", "text": "only one"},
        ])
        stat = tc.refresh_cache(projects_root=self.root, db_path=self.db, full=True)
        self.assertEqual(stat["reindexed"], 1)
        turns = tc.get_turns_since(
            0,
            projects_root=self.root,
            db_path=self.db,
            auto_refresh=False,
        )
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["text"], "only one")

    def test_cache_stats(self):
        import turns_cache as tc
        # DB 없음
        before = tc.cache_stats(db_path=self.db)
        self.assertFalse(before["exists"])
        # 빌드 후
        tc.refresh_cache(projects_root=self.root, db_path=self.db)
        after = tc.cache_stats(db_path=self.db)
        self.assertTrue(after["exists"])
        self.assertEqual(after["indexed_jsonl_files"], 1)
        self.assertGreater(after["indexed_turns"], 0)


if __name__ == "__main__":
    unittest.main()
