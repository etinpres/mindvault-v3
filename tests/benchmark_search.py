"""Sprint 4 성능 벤치마크. usage: python3 tests/benchmark_search.py

임베딩 서버(Arctic-ko) + production index.db (~100 memories) 기동 상태 가정.
"""
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "hooks" / "memory-recall.py"


def time_call(prompt: str) -> float:
    t0 = time.time()
    subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"prompt": prompt}).encode(),
        capture_output=True,
        timeout=5,
    )
    return (time.time() - t0) * 1000


def main():
    prompts = [
        "이메일 보내는 방법",
        "스캐너 사용법",
        "유튜브 영상 만드는 법",
        "택시 장부 IAP",
        "grammar saas",
        "polished html",
        "memory recall layer",
        "embedded search hybrid",
    ] * 13  # 104회

    print("warming up...")
    for _ in range(3):
        time_call("warmup")

    print(f"running {len(prompts)} iterations...")
    times = [time_call(p) for p in prompts]
    times_sorted = sorted(times)
    print()
    print(f"  n         = {len(times)}")
    print(f"  avg       = {statistics.mean(times):.1f} ms")
    print(f"  median    = {statistics.median(times):.1f} ms")
    print(f"  p95       = {times_sorted[int(len(times) * 0.95)]:.1f} ms")
    print(f"  p99       = {times_sorted[int(len(times) * 0.99)]:.1f} ms")
    print(f"  max       = {max(times):.1f} ms")
    print()

    avg = statistics.mean(times)
    p95 = times_sorted[int(len(times) * 0.95)]
    print(f"  target avg < 150ms — {'✓' if avg < 150 else '✗'}")
    print(f"  target p95 < 200ms — {'✓' if p95 < 200 else '✗'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
