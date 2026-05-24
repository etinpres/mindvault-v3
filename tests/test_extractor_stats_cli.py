"""Sprint NEXT-17 — extractor stats CLI 정규식·집계 검증."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

for _mod in ("extractor_stats_cli",):
    sys.modules.pop(_mod, None)


SAMPLE_LOG = """\
[2026-05-24 10:19:12] session-end: compiled session=949a8635 updates=0/2
[2026-05-24 10:19:12] session-end: session 949a8635: staged 2/2
[2026-05-24 10:25:13] session-end: no candidates for 949a8635
[2026-05-24 10:50:59] extractor: trigger=next1-action
[2026-05-24 10:51:00] extractor: trigger=keyword
[2026-05-24 10:51:01] extractor: trigger=next10-ack text='좋아!'
[2026-05-24 10:51:02] extractor: always-fire bypass for foo-sid.jsonl
[2026-05-24 10:51:03] extractor: no trigger in bar-sid.jsonl, skip
[2026-05-24 10:51:04] extractor: extract attempt=1/3 candidates=2
[2026-05-24 10:51:05] extractor: extract attempt=2/3 candidates=0
[2026-05-24 10:51:06] extractor: extract cache hit for baz-sid.jsonl: 3 candidates
[2026-05-24 10:51:07] session-end: jsonl missing for deadbeef
"""


class TestParseDebug(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        log = Path(self.tmp.name) / "debug.log"
        log.write_text(SAMPLE_LOG)
        sys.modules.pop("extractor_stats_cli", None)
        import extractor_stats_cli as esc
        esc.DEBUG_LOG = log
        self.esc = esc

    def tearDown(self):
        self.tmp.cleanup()

    def test_counts_all_categories(self):
        d = self.esc.parse_debug(None)
        self.assertEqual(d["jsonl_missing"], 1)
        self.assertEqual(d["no_trigger"], 1)
        self.assertEqual(d["no_candidates_after_trigger"], 1)
        self.assertEqual(d["always_fire_bypass"], 1)

    def test_trigger_layers(self):
        d = self.esc.parse_debug(None)
        layers = d["trigger_layers"]
        self.assertEqual(layers.get("next1-action"), 1)
        self.assertEqual(layers.get("keyword"), 1)
        self.assertEqual(layers.get("next10-ack"), 1)
        self.assertEqual(layers.get("always-fire"), 1)

    def test_attempts_stats(self):
        d = self.esc.parse_debug(None)
        a = d["attempts"]
        self.assertEqual(a["min"], 1)
        self.assertEqual(a["max"], 2)
        self.assertEqual(a["avg"], 1.5)

    def test_candidates_split(self):
        d = self.esc.parse_debug(None)
        c = d["candidates_per_extract"]
        # attempts: 2, 0 + cache_hit: 3 → zero=1, nonzero=2 (max=3)
        self.assertEqual(c["zero_count"], 1)
        self.assertEqual(c["nonzero_count"], 2)
        self.assertEqual(c["max"], 3)

    def test_cache_hit_counted(self):
        d = self.esc.parse_debug(None)
        self.assertEqual(d["cache_hit_count"], 1)

    def test_staged_pass_rate(self):
        d = self.esc.parse_debug(None)
        s = d["staged_pass_rate"]
        self.assertEqual(s["sessions"], 1)
        self.assertEqual(s["avg_pass_rate"], 1.0)

    def test_compiler_stats(self):
        d = self.esc.parse_debug(None)
        c = d["compiler"]
        self.assertEqual(c["sessions"], 1)
        self.assertEqual(c["total_updates"], 0)
        self.assertEqual(c["total_candidates"], 2)
        self.assertEqual(c["update_rate"], 0.0)


class TestEmptyLog(unittest.TestCase):
    def test_missing_log_graceful(self):
        sys.modules.pop("extractor_stats_cli", None)
        import extractor_stats_cli as esc
        esc.DEBUG_LOG = Path("/nonexistent/debug.log")
        d = esc.parse_debug(None)
        self.assertIn("error", d)


if __name__ == "__main__":
    unittest.main()
