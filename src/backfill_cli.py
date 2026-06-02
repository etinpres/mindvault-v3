#!/usr/bin/env python3
"""MindVault v3 NEXT-13 — SessionEnd hook backfill CLI.

debug.log 에 'jsonl missing for {sid[:8]}' 패턴으로 silent skip된 sid 들을
PROJECTS_ROOT.glob 으로 매칭해 다시 SessionEnd hook 직렬 호출. NEXT-8 fix
이후 운영 누적분 정리 + NEXT-10 extractor 효과 측정 인프라.

사용 예:
    python3 backfill_cli.py --dry-run                # 대상만 표시
    python3 backfill_cli.py --last-hours 168 --limit 50
    python3 backfill_cli.py --missing-only           # default
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# v3.2.7: production state pollution 방지. MV3_DATA_DIR / MV3_PROJECTS_ROOT / MV3_HOOKS_DIR env var 우선.
_MV3_DATA_DIR = Path(os.environ.get("MV3_DATA_DIR", "~/.claude/mindvault-v3")).expanduser()
PROJECTS_ROOT = Path(os.environ.get("MV3_PROJECTS_ROOT", "~/.claude/projects")).expanduser()
DEBUG_LOG = _MV3_DATA_DIR / "debug.log"
HOOK = Path(os.environ.get("MV3_HOOKS_DIR", "~/.claude/hooks")).expanduser() / "session-memory-end.py"

# debug.log 라인 형식: [2026-05-24 09:54:41] session-end: jsonl missing for 949a8635
MISSING_RE = re.compile(
    r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]\s+session-end:\s+jsonl missing for (\w{8})$"
)


def scan_missing(last_hours: float | None = None) -> list[tuple[datetime, str]]:
    """debug.log 에서 jsonl missing 이벤트 추출. (ts, sid_prefix) tuple list."""
    if not DEBUG_LOG.is_file():
        return []
    cutoff = (
        datetime.now() - timedelta(hours=last_hours) if last_hours else None
    )
    events: list[tuple[datetime, str]] = []
    with DEBUG_LOG.open() as f:
        for line in f:
            m = MISSING_RE.match(line.rstrip())
            if not m:
                continue
            # bug-audit 2026-06-02 (#24): 정규식(\d{2})은 달력상 불가능한 타임스탬프도
            # 매칭 → strptime ValueError 로 전체 스캔 중단. 손상 라인만 skip.
            try:
                ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                continue
            if cutoff and ts < cutoff:
                continue
            events.append((ts, m.group(2)))
    return events


def resolve_sid(prefix: str) -> list[tuple[str, str]]:
    """prefix → 모든 매칭 (full_sid, project_slot) 반환."""
    return [
        (h.stem, h.parent.name)
        for h in sorted(PROJECTS_ROOT.glob(f"*/{prefix}*.jsonl"))
    ]


def call_hook(sid: str, env: dict, timeout: int = 180) -> tuple[int, str]:
    """SessionEnd hook 한 번 호출. (exit_code, stderr) 반환."""
    payload = json.dumps({"sessionId": sid})
    try:
        p = subprocess.run(
            ["python3", str(HOOK)],
            input=payload,
            text=True,
            env=env,
            capture_output=True,
            timeout=timeout,
        )
        return p.returncode, p.stderr[:200]
    except subprocess.TimeoutExpired:
        return -1, "timeout"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--missing-only",
        action="store_true",
        default=True,
        help="debug.log 의 'jsonl missing' 패턴만 대상 (default)",
    )
    ap.add_argument(
        "--last-hours",
        type=float,
        default=None,
        help="debug.log timestamp 기준 최근 N 시간 (default: all)",
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        help="최대 N 건 처리 (default: 모두)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="hook 호출 안 함, target sid 만 출력",
    )
    ap.add_argument(
        "--sleep",
        type=float,
        default=0.7,
        help="hook 호출 간 sleep (sqlite WAL 충돌 회피, default 0.7)",
    )
    ap.add_argument(
        "--no-auto-compile",
        action="store_true",
        help="MV3_AUTO_COMPILE 강제 0 (compile fire 없이 staged 만)",
    )
    ap.add_argument(
        "--deep",
        action="store_true",
        help=(
            "NEXT-14 recall boost 풀세트: ALWAYS_FIRE=1 (trigger 우회) + "
            "TAIL_TURNS=120 + GEMMA_RETRIES=3. 매 sid 당 latency 30~90s. "
            "batch 누적 정리용 권장 (실시간 hook 부담은 default 보존)."
        ),
    )
    args = ap.parse_args(argv)

    # bug-audit 2026-06-01 (backfill-negative-args): 음수 인자는 부분실행+크래시
    # (--sleep<0 → time.sleep ValueError), 또는 조용한 오작동(--limit<0 → 최근 N건
    # 누락, --last-hours<0 → cutoff 미래로 전부 필터 → 'no events' 오인). fire 전 거부.
    if args.sleep < 0:
        ap.error("--sleep must be non-negative")
    if args.limit is not None and args.limit < 0:
        ap.error("--limit must be non-negative")
    if args.last_hours is not None and args.last_hours < 0:
        ap.error("--last-hours must be positive")

    events = scan_missing(args.last_hours)
    if not events:
        print("debug.log 에 'jsonl missing' 이벤트 없음.")
        return 0

    # prefix unique + 가장 최근 event 우선 (latest ts)
    seen: dict[str, datetime] = {}
    for ts, prefix in events:
        if prefix not in seen or ts > seen[prefix]:
            seen[prefix] = ts

    # resolve full sid
    resolved: list[tuple[str, str, str, datetime]] = []
    unresolved: list[tuple[str, datetime]] = []
    for prefix, ts in sorted(seen.items(), key=lambda x: x[1], reverse=True):
        hits = resolve_sid(prefix)
        if not hits:
            unresolved.append((prefix, ts))
            continue
        for full_sid, slot in hits:
            resolved.append((prefix, full_sid, slot, ts))

    if args.limit is not None:  # 0/None 구분 — --limit 0 은 0건 처리(falsy 버그 회피)
        resolved = resolved[: args.limit]

    print(f"scanned: {len(events)} events / {len(seen)} unique prefix")
    print(f"resolved: {len(resolved)} (jsonl 존재) / unresolved: {len(unresolved)} (jsonl 사라짐)")

    if args.dry_run:
        print("\n[DRY RUN] 호출 안 함. target sid:")
        for prefix, sid, slot, ts in resolved:
            print(f"  {ts.isoformat()} {prefix} -> {slot}")
        if unresolved:
            print("\n[DRY RUN] 사라진 sid (skip):")
            for prefix, ts in unresolved:
                print(f"  {ts.isoformat()} {prefix}")
        return 0

    env = os.environ.copy()
    env["MV3_AUTO_COMPILE"] = "0" if args.no_auto_compile else "1"
    if args.deep:
        env["MV3_EXTRACTOR_ALWAYS_FIRE"] = "1"
        env["MV3_EXTRACTOR_TAIL_TURNS"] = "120"
        env["MV3_EXTRACTOR_GEMMA_RETRIES"] = "3"
        print("--deep ON: ALWAYS_FIRE=1, TAIL_TURNS=120, GEMMA_RETRIES=3")

    stats = {"fire": 0, "ok": 0, "fail": []}
    for i, (prefix, sid, slot, ts) in enumerate(resolved, 1):
        print(f"\n[{i}/{len(resolved)}] {prefix} ({slot}) ts={ts.isoformat()}")
        code, err = call_hook(sid, env)
        stats["fire"] += 1
        if code == 0:
            stats["ok"] += 1
            print(f"  exit=0")
        else:
            stats["fail"].append((sid, code, err))
            print(f"  exit={code} stderr={err}")
        time.sleep(args.sleep)

    print(f"\n=== SUMMARY ===")
    print(f"fire: {stats['fire']}, ok: {stats['ok']}, fail: {len(stats['fail'])}")
    print(f"unresolved (jsonl 사라짐, skip): {len(unresolved)}")
    if stats["fail"]:
        for sid, code, err in stats["fail"]:
            print(f"  FAIL {sid[:8]}: code={code} {err}")
    return 0 if not stats["fail"] else 1


if __name__ == "__main__":
    sys.exit(main())
