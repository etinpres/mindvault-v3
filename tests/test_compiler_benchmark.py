"""compiler_benchmark — Gemma 호출은 mock, 통계 함수 검증."""
from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))


class TestP90(unittest.TestCase):
    def test_empty(self):
        from compiler_benchmark import _p90
        self.assertEqual(_p90([]), 0.0)

    def test_single(self):
        from compiler_benchmark import _p90
        self.assertEqual(_p90([42]), 42.0)

    def test_range(self):
        from compiler_benchmark import _p90
        # ceil(0.9*10)=9 → idx 8 → values[8]
        vals = list(range(10))  # 0..9
        self.assertEqual(_p90(vals), 8.0)


class TestRunOne(unittest.TestCase):
    def test_success(self):
        import compiler_benchmark
        with patch("memory_compiler._call_gemma", return_value="정제본"):
            r = compiler_benchmark.run_one(
                "short", "기존 body", {"title": "x", "body": "fact"}
            )
        self.assertTrue(r["success"])
        self.assertEqual(r["result_chars"], len("정제본"))
        self.assertGreaterEqual(r["elapsed_ms"], 0)

    def test_failure_none(self):
        import compiler_benchmark
        with patch("memory_compiler._call_gemma", return_value=None):
            r = compiler_benchmark.run_one(
                "short", "기존 body", {"title": "x", "body": "fact"}
            )
        self.assertFalse(r["success"])
        self.assertEqual(r["result_chars"], 0)


class TestBenchmark(unittest.TestCase):
    def test_aggregates(self):
        import compiler_benchmark
        with patch(
            "memory_compiler._call_gemma", return_value="aggregated mock"
        ):
            s = compiler_benchmark.benchmark(repeats=2)
        # 3 시나리오 × 2 = 6 runs
        self.assertEqual(s["total_runs"], 6)
        self.assertEqual(s["overall"]["success_rate"], 1.0)
        for label in ("short", "medium", "long"):
            self.assertIn(label, s["by_scenario"])
            self.assertEqual(s["by_scenario"][label]["n"], 2)

    def test_exception_path_recorded(self):
        import compiler_benchmark
        with patch(
            "memory_compiler._call_gemma",
            side_effect=RuntimeError("boom"),
        ):
            s = compiler_benchmark.benchmark(repeats=1)
        # _compile_update 의 try/except 가 None 잡으면 success=False
        # _call_gemma 자체가 try/except 내부에서 None 반환 → run_one 은 success=False
        self.assertEqual(s["total_runs"], 3)
        # 모두 실패 또는 정상 처리됐는지
        self.assertEqual(s["overall"]["success_rate"], 0.0)


class TestFormatReport(unittest.TestCase):
    def test_renders(self):
        import compiler_benchmark
        s = {
            "total_runs": 9,
            "overall": {
                "success_rate": 1.0, "latency_avg_ms": 1234,
                "latency_p50_ms": 1200, "latency_p90_ms": 1800,
                "latency_max_ms": 1900,
            },
            "by_scenario": {
                "short": {
                    "n": 3, "success_n": 3, "success_rate": 1.0,
                    "latency_avg_ms": 800, "latency_median_ms": 750,
                    "latency_min_ms": 700, "latency_max_ms": 900,
                    "result_chars_avg": 120,
                },
            },
        }
        out = compiler_benchmark.format_report(s)
        self.assertIn("total runs: 9", out)
        self.assertIn("short", out)
