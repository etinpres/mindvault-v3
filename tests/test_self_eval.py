"""Sprint 15 — Self-evaluation Loop 단위 테스트."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))


class TestParseTs(unittest.TestCase):
    def test_iso_z(self):
        from self_eval import _parse_ts
        out = _parse_ts("2026-05-22T17:05:26.819Z")
        self.assertIsNotNone(out)
        self.assertGreater(out, 1_700_000_000)

    def test_iso_naive(self):
        from self_eval import _parse_ts
        out = _parse_ts("2026-05-23T01:58:34")
        self.assertIsNotNone(out)

    def test_invalid(self):
        from self_eval import _parse_ts
        self.assertIsNone(_parse_ts(""))
        self.assertIsNone(_parse_ts("not a date"))


class TestNegativeCue(unittest.TestCase):
    def test_positive_cases(self):
        from self_eval import has_negative_cue
        cases = [
            "이거 관계없는 내용인데",
            "엉뚱한 메모리가 떠올랐네",
            "왜 이거 회수됐어?",
            "그게 아니라 다른 거야",
            "원하는 게 아니야",
            "쓸데없는 내용",
            "잘못 회수한 듯",
            "필요 없는데 이거",
        ]
        for txt in cases:
            self.assertTrue(
                has_negative_cue(txt),
                f"negative cue 누락: {txt!r}",
            )

    def test_negative_cases(self):
        from self_eval import has_negative_cue
        cases = [
            "회수 결과 좋다",
            "이거 맞아",
            "문제없이 잘 동작해",
            "다음 단계 진행하자",
            "",
        ]
        for txt in cases:
            self.assertFalse(
                has_negative_cue(txt),
                f"잘못된 negative cue: {txt!r}",
            )


class TestSelfAffirming(unittest.TestCase):
    def test_affirming_text(self):
        from self_eval import is_self_affirming
        text = "v2 운영 중 (품질 양호). 잘 작동하며 안정적."
        self.assertTrue(is_self_affirming(text))

    def test_below_threshold(self):
        from self_eval import is_self_affirming
        text = "한 번 잘 작동했어"
        self.assertFalse(is_self_affirming(text))

    def test_irrelevant(self):
        from self_eval import is_self_affirming
        self.assertFalse(is_self_affirming("로그에 에러 메시지 남음"))


class TestLoadRecallEvents(unittest.TestCase):
    def _write(self, lines: list[dict]) -> Path:
        f = tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
        )
        for d in lines:
            f.write(json.dumps(d) + "\n")
        f.close()
        return Path(f.name)

    def test_filters_non_recall(self):
        from self_eval import load_recall_events
        p = self._write([
            {"ts": "2026-05-23T01:00:00", "kind": "recall", "picked": 1},
            {"ts": "2026-05-23T01:00:01", "kind": "extract", "picked": 0},
            {"ts": "2026-05-23T01:00:02", "kind": "recall", "picked": 0},
        ])
        events = load_recall_events(p)
        self.assertEqual(len(events), 2)
        self.assertTrue(all(e["kind"] == "recall" for e in events))

    def test_sorted_and_ts_attached(self):
        from self_eval import load_recall_events
        p = self._write([
            {"ts": "2026-05-23T01:00:05", "kind": "recall", "picked": 1},
            {"ts": "2026-05-23T01:00:01", "kind": "recall", "picked": 1},
        ])
        events = load_recall_events(p)
        self.assertEqual(len(events), 2)
        self.assertLess(events[0]["_ts_unix"], events[1]["_ts_unix"])

    def test_missing_file_returns_empty(self):
        from self_eval import load_recall_events
        self.assertEqual(load_recall_events(Path("/no/such/path.jsonl")), [])


class TestLoadTurns(unittest.TestCase):
    def _write_jsonl(self, rows: list[dict]) -> Path:
        f = tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
        )
        for r in rows:
            f.write(json.dumps(r) + "\n")
        f.close()
        return Path(f.name)

    def test_skips_system_reminder_user(self):
        from self_eval import load_turns
        p = self._write_jsonl([
            {
                "type": "user",
                "timestamp": "2026-05-23T01:00:00Z",
                "message": {"content": "<system-reminder>x</system-reminder>"},
            },
            {
                "type": "user",
                "timestamp": "2026-05-23T01:00:01Z",
                "message": {"content": "real user msg"},
            },
        ])
        turns = load_turns(p)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["text"], "real user msg")

    def test_extracts_tool_uses(self):
        from self_eval import load_turns
        p = self._write_jsonl([
            {
                "type": "assistant",
                "timestamp": "2026-05-23T01:00:00Z",
                "message": {
                    "content": [
                        {"type": "text", "text": "thinking aloud"},
                        {"type": "tool_use", "name": "Bash", "input": {}},
                        {"type": "tool_use", "name": "Read", "input": {}},
                    ]
                },
            },
        ])
        turns = load_turns(p)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["tool_uses"], ["Bash", "Read"])

    def test_ignores_other_types(self):
        from self_eval import load_turns
        p = self._write_jsonl([
            {"type": "file-history-snapshot", "snapshot": {}},
            {
                "type": "user",
                "timestamp": "2026-05-23T01:00:00Z",
                "message": {"content": "hi"},
            },
        ])
        self.assertEqual(len(load_turns(p)), 1)


class TestMeasurePostRecall(unittest.TestCase):
    def test_counts_tool_uses_until_next_user(self):
        from self_eval import measure_post_recall
        turns = [
            {"ts_unix": 1000, "role": "user", "text": "trigger", "tool_uses": []},
            {
                "ts_unix": 1005,
                "role": "assistant",
                "text": "ok",
                "tool_uses": ["Read", "Bash"],
            },
            {
                "ts_unix": 1010,
                "role": "assistant",
                "text": "",
                "tool_uses": ["Bash"],
            },
            {"ts_unix": 1020, "role": "user", "text": "관계없는데", "tool_uses": []},
            {
                "ts_unix": 1025,
                "role": "assistant",
                "text": "",
                "tool_uses": ["Read"],  # 이건 다음 recall 의 영역
            },
        ]
        out = measure_post_recall(turns, recall_ts=1001)
        self.assertEqual(out["tool_use_count"], 3)  # Read, Bash, Bash
        self.assertEqual(
            out["tool_use_breakdown"], {"Read": 1, "Bash": 2}
        )
        self.assertEqual(out["next_user_text"], "관계없는데")

    def test_no_next_user(self):
        from self_eval import measure_post_recall
        turns = [
            {"ts_unix": 1000, "role": "user", "text": "x", "tool_uses": []},
            {
                "ts_unix": 1005, "role": "assistant", "text": "", "tool_uses": ["Bash"],
            },
        ]
        out = measure_post_recall(turns, recall_ts=1001)
        self.assertEqual(out["tool_use_count"], 1)
        self.assertIsNone(out["next_user_text"])


class TestScanSelfAffirming(unittest.TestCase):
    def test_finds_affirming(self):
        from self_eval import scan_self_affirming_memories
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "good.md").write_text(
                "---\nname: project ok\n---\n"
                "운영 중인 시스템. 잘 작동하며 안정적이라 문제없이 돌아간다.",
                encoding="utf-8",
            )
            (d / "boring.md").write_text(
                "---\nname: notes\n---\n그냥 메모", encoding="utf-8",
            )
            out = scan_self_affirming_memories(memory_dirs=[d])
            self.assertEqual(len(out), 1)
            self.assertEqual(out[0]["name"], "project ok")
            self.assertGreaterEqual(out[0]["hit_count"], 2)


class TestAnalyzeRecentIntegration(unittest.TestCase):
    """end-to-end: 가짜 metrics + 가짜 session jsonl → 기대 metric 산출."""

    def test_full_pipeline(self):
        """metric ts 와 jsonl ts 의 timezone 정합성 — production 에선 hook 작성 시점이
        동일하므로 metric naive(local) ↔ jsonl Z(UTC) 가 같은 unix 로 변환된다.
        테스트는 격리상 둘 다 명시 tz 통일(Z) 로 검증."""
        import self_eval
        with tempfile.TemporaryDirectory() as tmp_metrics_root, \
             tempfile.TemporaryDirectory() as tmp_proj_root:
            metrics_path = Path(tmp_metrics_root) / "metrics.jsonl"
            # 두 recall ts 모두 UTC Z 명시 — jsonl 과 같은 정렬
            metrics_path.write_text(
                json.dumps({
                    "ts": "2026-05-23T01:00:00Z",
                    "kind": "recall",
                    "picked": 1,
                    "raw_top1_cosine": 0.5,
                    "raw_min": 0.4,
                }) + "\n" + json.dumps({
                    "ts": "2026-05-23T01:10:00Z",
                    "kind": "recall",
                    "picked": 0,
                    "raw_top1_cosine": 0.2,
                    "raw_min": 0.4,
                }) + "\n",
                encoding="utf-8",
            )
            projects_root = Path(tmp_proj_root)
            (projects_root / "session1").mkdir()
            jsonl = projects_root / "session1" / "abc.jsonl"
            base_iso = "2026-05-23T01:00"
            jsonl.write_text(
                json.dumps({
                    "type": "user",
                    "timestamp": f"{base_iso}:00Z",
                    "message": {"content": "trigger query"},
                }) + "\n" + json.dumps({
                    "type": "assistant",
                    "timestamp": f"{base_iso}:01Z",
                    "message": {"content": [
                        {"type": "tool_use", "name": "Bash"},
                        {"type": "tool_use", "name": "Read"},
                    ]},
                }) + "\n" + json.dumps({
                    "type": "user",
                    "timestamp": f"{base_iso}:30Z",
                    "message": {"content": "이거 관계없는데"},
                }) + "\n",
                encoding="utf-8",
            )
            # hours_back 매우 크게 (현재 시점에서 과거 ts 가 윈도우 안에 들도록)
            summary = self_eval.analyze_recent(
                metrics_path=metrics_path,
                projects_root=projects_root,
                hours_back=24 * 365 * 5,
            )
            self.assertEqual(summary["total_recalls"], 2)
            self.assertEqual(summary["recalls_with_pick"], 1)
            self.assertAlmostEqual(summary["hit_rate"], 0.5)
            # 첫 recall 의 다음 user 가 negative cue
            self.assertGreaterEqual(summary["false_positive_count"], 1)


class TestFormatReport(unittest.TestCase):
    def test_format_renders(self):
        from self_eval import format_report
        summary = {
            "hours_back": 168,
            "total_recalls": 10,
            "recalls_with_pick": 7,
            "hit_rate": 0.7,
            "avg_internal_effort": 1.4,
            "false_positive_rate": 0.1,
            "false_positive_count": 1,
            "false_positive_known": 10,
            "self_affirming_memories": [
                {"name": "x", "hit_count": 3, "sample_terms": ["잘 작동"]}
            ],
        }
        out = format_report(summary)
        self.assertIn("hit rate: 70.0%", out)
        self.assertIn("self-affirming", out)


if __name__ == "__main__":
    unittest.main()
