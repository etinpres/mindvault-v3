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

    def test_skips_hook_injected_prefix(self):
        """SessionStart hook 의 Gemma system prompt 가 user role 로 들어와도 skip."""
        from self_eval import load_turns
        p = self._write_jsonl([
            {
                "type": "user",
                "timestamp": "2026-05-23T01:00:00Z",
                "message": {"content": "다음은 Claude Code 세션 1개의 발췌입니다..."},
            },
            {
                "type": "user",
                "timestamp": "2026-05-23T01:00:01Z",
                "message": {"content": "# 지난 세션 요약\n블라블라"},
            },
            {
                "type": "user",
                "timestamp": "2026-05-23T01:00:02Z",
                "message": {"content": "진짜 user query"},
            },
        ])
        turns = load_turns(p)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["text"], "진짜 user query")

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
                "tool_uses": ["Read"],
            },
        ]
        out = measure_post_recall(turns, recall_ts=1001)
        self.assertEqual(out["tool_use_count"], 3)
        self.assertEqual(
            out["tool_use_breakdown"], {"Read": 1, "Bash": 2}
        )
        self.assertEqual(out["next_user_text"], "관계없는데")
        self.assertEqual(out["next_user_chars"], len("관계없는데"))
        self.assertFalse(out["abandoned"])

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
        self.assertEqual(out["next_user_chars"], -1)
        self.assertTrue(out["abandoned"])


class TestImplicitFpSignal(unittest.TestCase):
    def test_short_next_user(self):
        from self_eval import implicit_fp_signal
        post = {"abandoned": False, "next_user_chars": 5}
        self.assertEqual(implicit_fp_signal(post), "short_next_user")

    def test_abandoned(self):
        from self_eval import implicit_fp_signal
        post = {"abandoned": True, "next_user_chars": -1}
        self.assertEqual(implicit_fp_signal(post), "abandoned")

    def test_normal_no_signal(self):
        from self_eval import implicit_fp_signal
        post = {"abandoned": False, "next_user_chars": 50}
        self.assertIsNone(implicit_fp_signal(post))

    def test_exact_boundary(self):
        """경계값 14자 → short, 15자 → no signal."""
        from self_eval import implicit_fp_signal, SHORT_NEXT_USER_CHARS
        post14 = {"abandoned": False, "next_user_chars": SHORT_NEXT_USER_CHARS - 1}
        post15 = {"abandoned": False, "next_user_chars": SHORT_NEXT_USER_CHARS}
        self.assertEqual(implicit_fp_signal(post14), "short_next_user")
        self.assertIsNone(implicit_fp_signal(post15))


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
            "internal_effort": {
                "avg": 1.4, "p50": 1.0, "p90": 4.0, "p99": 7.0, "max": 7,
                "histogram": {"0": 3, "1": 4, "2-4": 2, "5+": 1},
                "long_tail_ratio": 0.1,
            },
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
        self.assertIn("p50=1", out)
        self.assertIn("long-tail ratio", out)


class TestEffortStats(unittest.TestCase):
    def test_empty(self):
        from self_eval import _effort_stats
        r = _effort_stats([])
        self.assertEqual(r["n"], 0)
        self.assertEqual(r["avg"], 0.0)
        self.assertEqual(r["long_tail_ratio"], 0.0)

    def test_histogram_bucketing(self):
        from self_eval import _effort_stats
        # 0×5, 1×3, 3×2, 7×1, 12×1
        vals = [0, 0, 0, 0, 0, 1, 1, 1, 3, 3, 7, 12]
        r = _effort_stats(vals)
        self.assertEqual(r["histogram"], {"0": 5, "1": 3, "2-4": 2, "5+": 2})
        self.assertEqual(r["max"], 12)
        self.assertEqual(r["long_tail_ratio"], 2 / 12)

    def test_percentile_simple(self):
        from self_eval import _effort_stats
        vals = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]  # n=10
        r = _effort_stats(vals)
        # nearest-rank: p50 → ceil(0.5*10)=5 → idx 4 → 4
        self.assertEqual(r["p50"], 4.0)
        # p90 → ceil(0.9*10)=9 → idx 8 → 8
        self.assertEqual(r["p90"], 8.0)
        self.assertEqual(r["p99"], 9.0)

    def test_all_zero(self):
        from self_eval import _effort_stats
        r = _effort_stats([0, 0, 0])
        self.assertEqual(r["avg"], 0.0)
        self.assertEqual(r["p90"], 0.0)
        self.assertEqual(r["long_tail_ratio"], 0.0)

    def test_long_tail_heavy(self):
        from self_eval import _effort_stats
        # 5+ 비율 80%
        r = _effort_stats([5, 6, 7, 8, 1])
        self.assertAlmostEqual(r["long_tail_ratio"], 0.8)


class TestPercentile(unittest.TestCase):
    def test_edge_cases(self):
        from self_eval import _percentile
        self.assertEqual(_percentile([], 50), 0.0)
        self.assertEqual(_percentile([7], 0), 7.0)
        self.assertEqual(_percentile([7], 100), 7.0)
        self.assertEqual(_percentile([1, 2, 3], 50), 2.0)


class TestIntentStats(unittest.TestCase):
    def test_basic_distribution(self):
        from self_eval import _intent_stats_from_events
        recall = [
            {"intent": "code", "picked": 1},
            {"intent": "code", "picked": 0},
            {"intent": "recall", "picked": 1},
            {"intent": "unknown", "picked": 0},
        ]
        skip = [
            {"intent": "chat"},
            {"intent": "chat"},
            {"intent": "meta"},
        ]
        s = _intent_stats_from_events(recall, skip)
        self.assertEqual(s["total_attempts"], 4)
        self.assertEqual(s["total_skipped"], 3)
        # code: 2 attempts, 1 picked → hit_rate 50%
        self.assertEqual(s["by_intent"]["code"]["recall_attempts"], 2)
        self.assertEqual(s["by_intent"]["code"]["picked"], 1)
        self.assertAlmostEqual(s["by_intent"]["code"]["hit_rate"], 0.5)
        # chat: skip 만
        self.assertEqual(s["by_intent"]["chat"]["skipped"], 2)
        self.assertEqual(s["by_intent"]["chat"]["recall_attempts"], 0)
        # 전체 skip_ratio = 3/7
        self.assertAlmostEqual(s["skip_ratio_of_all"], 3 / 7)

    def test_pre_sprint16_bucket(self):
        """intent 필드 없는 옛 recall 은 'pre-sprint16' bucket 에 모임."""
        from self_eval import _intent_stats_from_events
        recall = [{"picked": 1}, {"picked": 0}]  # intent 누락
        s = _intent_stats_from_events(recall, [])
        self.assertIn("pre-sprint16", s["by_intent"])
        self.assertEqual(s["by_intent"]["pre-sprint16"]["recall_attempts"], 2)

    def test_empty(self):
        from self_eval import _intent_stats_from_events
        s = _intent_stats_from_events([], [])
        self.assertEqual(s["total_attempts"], 0)
        self.assertEqual(s["total_skipped"], 0)
        self.assertEqual(s["skip_ratio_of_all"], 0.0)


class TestLoadRecallEventsKindFilter(unittest.TestCase):
    def test_filters_by_kinds_tuple(self):
        from self_eval import load_recall_events
        f = tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
        )
        for d in [
            {"ts": "2026-05-23T01:00:00Z", "kind": "recall"},
            {"ts": "2026-05-23T01:00:01Z", "kind": "recall_skip"},
            {"ts": "2026-05-23T01:00:02Z", "kind": "recall"},
        ]:
            f.write(json.dumps(d) + "\n")
        f.close()
        only_recall = load_recall_events(Path(f.name))
        only_skip = load_recall_events(Path(f.name), kinds=("recall_skip",))
        self.assertEqual(len(only_recall), 2)
        self.assertEqual(len(only_skip), 1)
        self.assertEqual(only_skip[0]["kind"], "recall_skip")


class TestExtractCommandBin(unittest.TestCase):
    def test_simple_command(self):
        from self_eval import _extract_command_bin
        self.assertEqual(_extract_command_bin("git status"), "git")
        self.assertEqual(_extract_command_bin("python3 -m pytest"), "python3")
        self.assertEqual(
            _extract_command_bin("claude --bg 'do this'"), "claude"
        )

    def test_skip_builtins(self):
        from self_eval import _extract_command_bin
        self.assertIsNone(_extract_command_bin("ls -la"))
        self.assertIsNone(_extract_command_bin("cat file.txt"))
        self.assertIsNone(_extract_command_bin("cd /tmp"))
        self.assertIsNone(_extract_command_bin("rm -rf x"))

    def test_skip_env_assignment(self):
        from self_eval import _extract_command_bin
        # 'X=Y' 가 첫 token 이면 명령어 아님
        self.assertIsNone(_extract_command_bin("PATH=/x foo"))

    def test_multiline_first_only(self):
        from self_eval import _extract_command_bin
        self.assertEqual(
            _extract_command_bin("npm install\nnpm run build"),
            "npm",
        )


class TestAuditProceduralCoverage(unittest.TestCase):
    def _write_jsonl(self, path: Path, rows: list[dict]) -> Path:
        path.write_text(
            "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
        )
        return path

    def test_coverage_basic(self):
        from self_eval import audit_procedural_coverage
        with tempfile.TemporaryDirectory() as proj_tmp, \
             tempfile.TemporaryDirectory() as mem_tmp:
            proj_root = Path(proj_tmp)
            (proj_root / "s1").mkdir()
            self._write_jsonl(proj_root / "s1" / "a.jsonl", [
                {
                    "type": "assistant",
                    "timestamp": "2099-01-01T00:00:00Z",
                    "message": {"content": [
                        {
                            "type": "tool_use", "name": "Bash",
                            "input": {"command": "claude --bg 'foo'"},
                        },
                        {
                            "type": "tool_use", "name": "Bash",
                            "input": {"command": "git worktree add x"},
                        },
                        {
                            "type": "tool_use", "name": "Read",
                            "input": {"file_path": "/x"},  # Bash 아님 → skip
                        },
                    ]},
                },
            ])
            mem_dir = Path(mem_tmp)
            # procedural 슬롯 — claude 명령어 메모리
            proc = mem_dir / "_procedural"
            proc.mkdir()
            (proc / "claude_bg.md").write_text(
                "---\nname: claude bg\ntype: procedural\n---\n"
                "claude --bg \"prompt\" — 백그라운드 세션",
                encoding="utf-8",
            )
            # 일반 슬롯 — git 명령어 본문
            (mem_dir / "git_notes.md").write_text(
                "---\nname: git notes\n---\ngit worktree add 패턴",
                encoding="utf-8",
            )
            out = audit_procedural_coverage(
                projects_root=proj_root,
                memory_dirs=[mem_dir],
                hours_back=24 * 365 * 100,
                top_n=5,
            )
            self.assertEqual(out["total_bash_commands_examined"], 2)
            # procedural_memory_count: _procedural slot 의 claude_bg (1건)
            # git_notes 는 type=procedural 아니라 카운트 X
            self.assertEqual(out["procedural_memory_count"], 1)
            # claude 매칭 — covered. git: 본문에 'git worktree' 있지만
            # procedural 후보 0건이라 git 은 매칭 후보가 git_notes 가 아닌
            # _procedural slot 의 claude_bg 만 본다. claude_bg body 에 'git' 없음 → 미커버
            covered_cmds = {row["command"] for row in out["table"] if row["covered"]}
            self.assertEqual(covered_cmds, {"claude"})
            self.assertIn("git", {row["command"] for row in out["table"]})

    def test_no_commands(self):
        from self_eval import audit_procedural_coverage
        with tempfile.TemporaryDirectory() as proj_tmp:
            out = audit_procedural_coverage(
                projects_root=Path(proj_tmp), memory_dirs=[],
            )
            self.assertEqual(out["total_bash_commands_examined"], 0)
            self.assertEqual(out["unique_binaries"], 0)
            self.assertEqual(out["coverage_ratio"], 0.0)


class TestClassifyUserTurns(unittest.TestCase):
    def test_classifies_and_buckets(self):
        from self_eval import classify_user_turns
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "s1").mkdir()
            jsonl = root / "s1" / "abc.jsonl"
            jsonl.write_text(
                json.dumps({
                    "type": "user", "timestamp": "2099-01-01T00:00:00Z",
                    "message": {"content": "안녕하세요"},
                }) + "\n"
                + json.dumps({
                    "type": "user", "timestamp": "2099-01-01T00:00:01Z",
                    "message": {"content": "이 함수 고쳐줘"},
                }) + "\n"
                + json.dumps({
                    "type": "user", "timestamp": "2099-01-01T00:00:02Z",
                    "message": {"content": "예전에 했던 거 기억나"},
                }) + "\n",
                encoding="utf-8",
            )
            out = classify_user_turns(
                projects_root=root, hours_back=24 * 365 * 100
            )
            self.assertEqual(out["total_user_turns_examined"], 3)
            by = out["by_intent"]
            self.assertEqual(by["chat"]["count"], 1)
            self.assertEqual(by["code"]["count"], 1)
            self.assertEqual(by["recall"]["count"], 1)


class TestIdToMatchToken(unittest.TestCase):
    """NEXT-37 Phase 1B — recalled_id → assistant text 매칭용 token 변환."""

    def test_name_slug_unchanged(self):
        from self_eval import _id_to_match_token
        self.assertEqual(_id_to_match_token("project-mindvault"), "project-mindvault")

    def test_path_basename_without_ext(self):
        from self_eval import _id_to_match_token
        self.assertEqual(
            _id_to_match_token("/a/b/feedback_recalled_memory_weight.md"),
            "feedback_recalled_memory_weight",
        )

    def test_relative_path_basename(self):
        from self_eval import _id_to_match_token
        self.assertEqual(
            _id_to_match_token("memory/project_mindvault.md"),
            "project_mindvault",
        )

    def test_empty_string(self):
        from self_eval import _id_to_match_token
        self.assertEqual(_id_to_match_token(""), "")


class TestClassifyRecallUtilization(unittest.TestCase):
    """NEXT-37 Phase 1B — 답변 cite 4-bucket 분류."""

    def test_no_response_when_text_none(self):
        from self_eval import classify_recall_utilization
        out = classify_recall_utilization(None, ["project-mindvault"])
        self.assertEqual(out["status"], "no_response")
        self.assertEqual(out["cited_ids"], [])
        self.assertFalse(out["has_recall_marker"])

    def test_unused_when_no_mention_no_marker(self):
        from self_eval import classify_recall_utilization
        out = classify_recall_utilization(
            "오늘 날씨 좋네", ["project-mindvault", "feedback-x"]
        )
        self.assertEqual(out["status"], "unused")
        self.assertEqual(out["cited_ids"], [])

    def test_marker_only_when_alert_but_no_id_mention(self):
        from self_eval import classify_recall_utilization
        out = classify_recall_utilization(
            "→ 메모리 회수: 관련 메모 없음. 본 답변은 신규 작업.",
            ["project-mindvault"],
        )
        self.assertEqual(out["status"], "marker_only")
        self.assertEqual(out["cited_ids"], [])
        self.assertTrue(out["has_recall_marker"])

    def test_cited_when_id_substring_found(self):
        from self_eval import classify_recall_utilization
        out = classify_recall_utilization(
            "[[project-mindvault]] 의 v3.2.10 단락 보면...",
            ["project-mindvault", "feedback-x"],
        )
        self.assertEqual(out["status"], "cited")
        self.assertEqual(out["cited_ids"], ["project-mindvault"])

    def test_cited_with_path_basename_match(self):
        from self_eval import classify_recall_utilization
        out = classify_recall_utilization(
            "feedback_recalled_memory_weight 메모리에 적혀 있음",
            ["/a/b/feedback_recalled_memory_weight.md"],
        )
        self.assertEqual(out["status"], "cited")
        self.assertEqual(len(out["cited_ids"]), 1)

    def test_cited_case_insensitive(self):
        from self_eval import classify_recall_utilization
        out = classify_recall_utilization(
            "PROJECT-MINDVAULT 라고 대문자로 mention",
            ["project-mindvault"],
        )
        self.assertEqual(out["status"], "cited")


class TestNextAssistantText(unittest.TestCase):
    """NEXT-37 Phase 1B — recall_ts 직후 first assistant turn 추출."""

    def test_returns_first_assistant_in_window(self):
        from self_eval import next_assistant_text
        turns = [
            {"ts_unix": 100.0, "role": "user", "text": "질문"},
            {"ts_unix": 101.0, "role": "assistant", "text": "첫 답변"},
            {"ts_unix": 110.0, "role": "assistant", "text": "두번째"},
        ]
        self.assertEqual(next_assistant_text(turns, 100.5), "첫 답변")

    def test_skips_turns_before_recall_ts(self):
        from self_eval import next_assistant_text
        turns = [
            {"ts_unix": 50.0, "role": "assistant", "text": "옛날"},
            {"ts_unix": 200.0, "role": "assistant", "text": "새 답변"},
        ]
        self.assertEqual(next_assistant_text(turns, 100.0), "새 답변")

    def test_returns_none_beyond_window(self):
        from self_eval import next_assistant_text
        turns = [
            {"ts_unix": 1000.0, "role": "assistant", "text": "너무 늦음"},
        ]
        # window_sec=300 default. recall_ts=100 → 1000-100=900 > 300 → None
        self.assertIsNone(next_assistant_text(turns, 100.0))

    def test_skips_user_turns(self):
        from self_eval import next_assistant_text
        turns = [
            {"ts_unix": 101.0, "role": "user", "text": "후속"},
            {"ts_unix": 102.0, "role": "assistant", "text": "답"},
        ]
        self.assertEqual(next_assistant_text(turns, 100.0), "답")

    def test_skips_empty_text_assistant(self):
        from self_eval import next_assistant_text
        turns = [
            {"ts_unix": 101.0, "role": "assistant", "text": ""},
            {"ts_unix": 102.0, "role": "assistant", "text": "실제 답"},
        ]
        self.assertEqual(next_assistant_text(turns, 100.0), "실제 답")


class TestRecallUtilizationEndToEnd(unittest.TestCase):
    """NEXT-37 Phase 1B — fake metrics + fake jsonl 1쌍 → 분류 1건 확인."""

    def test_single_cited_event(self):
        from self_eval import recall_utilization
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            data = tmp_p / "data"
            projects = tmp_p / "projects" / "-Users-x"
            data.mkdir(parents=True)
            projects.mkdir(parents=True)
            metrics = data / "metrics.jsonl"
            # tz-agnostic test: metrics ts 에도 Z 명시. production hook 은 naive
            # local time 으로 박지만, jsonl Z 와 같은 unix 가 나오게 하려면 KST
            # 환경에서만 동작. CI/다른 tz 격리 위해 둘 다 UTC Z 통일.
            metrics.write_text(json.dumps({
                "ts": "2099-01-01T00:00:00Z",
                "kind": "recall",
                "picked": 1,
                "recalled_ids": ["project-mindvault"],
            }) + "\n", encoding="utf-8")
            (projects / "sess-1.jsonl").write_text(
                json.dumps({
                    "type": "user", "timestamp": "2099-01-01T00:00:00Z",
                    "message": {"content": "v4 비전 어디까지 했지"},
                }) + "\n"
                + json.dumps({
                    "type": "assistant", "timestamp": "2099-01-01T00:00:05Z",
                    "message": {"content": "[[project-mindvault]] 의 v4 단락 보면..."},
                }) + "\n",
                encoding="utf-8",
            )
            out = recall_utilization(
                metrics_path=metrics,
                projects_root=tmp_p / "projects",
                hours_back=24 * 365 * 200,
                use_cache=False,
            )
            self.assertEqual(out["total_with_ids"], 1)
            self.assertEqual(out["by_status"]["cited"], 1)
            self.assertEqual(out["by_status"]["unused"], 0)
            self.assertEqual(out["utilization_rate_strict"], 1.0)
            self.assertEqual(
                out["per_event_sample"][0]["cited_ids"], ["project-mindvault"]
            )

    def test_unused_when_text_unrelated(self):
        from self_eval import recall_utilization
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            data = tmp_p / "data"
            projects = tmp_p / "projects" / "-Users-x"
            data.mkdir(parents=True)
            projects.mkdir(parents=True)
            metrics = data / "metrics.jsonl"
            metrics.write_text(json.dumps({
                "ts": "2099-01-02T00:00:00Z",
                "kind": "recall",
                "picked": 1,
                "recalled_ids": ["project-mindvault"],
            }) + "\n", encoding="utf-8")
            (projects / "sess-2.jsonl").write_text(
                json.dumps({
                    "type": "user", "timestamp": "2099-01-02T00:00:00Z",
                    "message": {"content": "오늘 점심 뭐 먹지"},
                }) + "\n"
                + json.dumps({
                    "type": "assistant", "timestamp": "2099-01-02T00:00:05Z",
                    "message": {"content": "라면 어때"},
                }) + "\n",
                encoding="utf-8",
            )
            out = recall_utilization(
                metrics_path=metrics,
                projects_root=tmp_p / "projects",
                hours_back=24 * 365 * 200,
                use_cache=False,
            )
            self.assertEqual(out["by_status"]["unused"], 1)
            self.assertEqual(out["by_status"]["cited"], 0)
            self.assertEqual(out["utilization_rate_strict"], 0.0)


if __name__ == "__main__":
    unittest.main()
