#!/usr/bin/env python3
"""MindVault v3 — Memory Compiler latency benchmark.

Sprint 15 BUILD-LOG §"미해결" #5 해소.

Sprint 14 의 Memory Compiler 는 opt-in (MV2_AUTO_COMPILE=1) 상태라 운영 fire 0건.
형이 켜기 전에 Gemma 정제 호출의 latency·성공률·응답 길이 분포를 안전하게 측정.

설계:
- 3 시나리오 (short/medium/long) × N회 반복 호출
- _compile_update 직접 호출 (실제 메모리·DB·staged 디렉토리 건드리지 않음)
- 각 호출 latency 측정 + 결과 본문 길이 + None 여부

사용:
  python3 compiler_benchmark.py [--repeats 3] [--json]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))


# 가짜 시나리오: (label, existing_body, candidate)
SCENARIOS = [
    (
        "short",
        "claude --bg 명령어로 백그라운드 세션 시작. 결과는 jobs 디렉토리.",
        {
            "title": "claude bg syntax",
            "body": "claude --bg 'prompt' --resume <id> 로 기존 세션 이어서 가능.",
        },
    ),
    (
        "medium",
        (
            "Memory Compiler 패턴 — Sprint 14 도입.\n\n"
            "SessionEnd 에서 extractor 가 뽑은 candidate 를 staged 에 던지지 않고\n"
            "기존 memory 와 매칭 단계 추가. 매칭 있으면 Gemma 가 기존 본문 + 새 fact 통합.\n"
            "검토는 /memory_review diff <file> 로 unified diff. approve 시 .bak 백업."
        ),
        {
            "title": "Memory Compiler",
            "body": (
                "opt-in env MV2_AUTO_COMPILE=1 로 활성. 매칭은 frontmatter name 우선, "
                "slug fallback. update_of/diff_summary 메타 부착."
            ),
        },
    ),
    (
        "long",
        (
            "MindVault v2 → v3 진행 노트.\n\n"
            "Sprint 13: procedural memory slot 신설 — memory/_procedural/ 디렉토리, "
            "frontmatter type=procedural 새 valid type.\n"
            "Sprint 14: Memory Compiler — LLM-as-compiler 패턴, Gemma 정제, opt-in.\n"
            "Sprint 15: self-evaluation loop — metrics.jsonl + JSONL 분석. "
            "hit rate, internal effort, false positive, self-affirming memory 4가지 metric.\n"
            "Sprint 16: query intent classifier (rule-based) — hook chat/meta 강제 skip. "
            "multi-source 영구 등록 sources.json.\n\n"
            "운영 실측: hit rate 66.5%, false positive 0%, internal effort 0.60 (이전 측정).\n"
            "Karpathy LLM Wiki 가 이론 토대. RAG 대신 LLM 이 raw → wiki 정제."
        ),
        {
            "title": "MindVault v3 progress",
            "body": (
                "Sprint 17 ship 형 결정 영역. 운영 sync 완료. dedup_cli 인프라 추가. "
                "test isolation 5건 fix. handoff/V3-PLAN 정식 add. classifier 분포 측정. "
                "internal effort 빈 user turn 필터 후 avg 21+. procedural coverage 0%."
            ),
        },
    ),
]


def run_one(scenario_label: str, existing_body: str, candidate: dict) -> dict:
    """단일 시나리오 1회 측정."""
    from memory_compiler import _compile_update  # noqa: WPS433
    t0 = time.time()
    out = _compile_update(existing_body, candidate)
    elapsed_ms = int((time.time() - t0) * 1000)
    return {
        "scenario": scenario_label,
        "elapsed_ms": elapsed_ms,
        "existing_chars": len(existing_body),
        "candidate_chars": len(candidate.get("body") or ""),
        "result_chars": len(out) if out else 0,
        "success": out is not None,
    }


def benchmark(repeats: int = 3) -> dict:
    runs: list[dict] = []
    for label, existing, cand in SCENARIOS:
        for _i in range(repeats):
            try:
                runs.append(run_one(label, existing, cand))
            except Exception as e:
                runs.append({
                    "scenario": label,
                    "elapsed_ms": -1,
                    "success": False,
                    "error": f"{type(e).__name__}: {e}",
                })

    by_scenario: dict[str, list[dict]] = {}
    for r in runs:
        by_scenario.setdefault(r["scenario"], []).append(r)

    summary: dict = {"total_runs": len(runs), "by_scenario": {}}
    all_latencies: list[int] = []
    success_count = 0
    for label, rs in by_scenario.items():
        latencies = [r["elapsed_ms"] for r in rs if r["elapsed_ms"] >= 0]
        all_latencies.extend(latencies)
        succ = [r for r in rs if r["success"]]
        success_count += len(succ)
        summary["by_scenario"][label] = {
            "n": len(rs),
            "success_n": len(succ),
            "success_rate": (len(succ) / len(rs)) if rs else 0.0,
            "latency_avg_ms": (
                statistics.mean(latencies) if latencies else 0.0
            ),
            "latency_median_ms": (
                statistics.median(latencies) if latencies else 0.0
            ),
            "latency_min_ms": min(latencies) if latencies else 0,
            "latency_max_ms": max(latencies) if latencies else 0,
            "result_chars_avg": (
                statistics.mean([r["result_chars"] for r in succ])
                if succ else 0.0
            ),
        }
    summary["overall"] = {
        "success_rate": (
            success_count / len(runs) if runs else 0.0
        ),
        "latency_avg_ms": (
            statistics.mean(all_latencies) if all_latencies else 0.0
        ),
        "latency_p50_ms": (
            statistics.median(all_latencies) if all_latencies else 0.0
        ),
        "latency_p90_ms": _p90(all_latencies),
        "latency_max_ms": max(all_latencies) if all_latencies else 0,
    }
    return summary


def _p90(values: list[int]) -> float:
    if not values:
        return 0.0
    sv = sorted(values)
    import math
    rank = max(0, math.ceil(0.9 * len(sv)) - 1)
    return float(sv[rank])


def format_report(s: dict) -> str:
    lines = [
        "# Memory Compiler Benchmark",
        f"total runs: {s['total_runs']}",
        f"overall success rate: {s['overall']['success_rate']*100:.1f}%",
        f"overall latency: avg={s['overall']['latency_avg_ms']:.0f}ms  "
        f"p50={s['overall']['latency_p50_ms']:.0f}ms  "
        f"p90={s['overall']['latency_p90_ms']:.0f}ms  "
        f"max={s['overall']['latency_max_ms']}ms",
        "",
        "per scenario:",
    ]
    for label, v in s["by_scenario"].items():
        lines.append(
            f"  {label:8s} n={v['n']}  succ={v['success_n']}/{v['n']}  "
            f"latency avg={v['latency_avg_ms']:.0f}ms "
            f"med={v['latency_median_ms']:.0f}ms  "
            f"min={v['latency_min_ms']}ms max={v['latency_max_ms']}ms  "
            f"result_chars avg={v['result_chars_avg']:.0f}"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        s = benchmark(repeats=args.repeats)
        if args.json:
            json.dump(s, sys.stdout, ensure_ascii=False, indent=2)
        else:
            print(format_report(s))
        return 0
    except Exception as e:
        print(f"benchmark failed: {e}", file=sys.stderr)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
