#!/usr/bin/env python3
"""MindVault v3 Sprint NEXT-7 — turns cache.

self_eval --hours 168 의 50초 latency 를 캐시로 <5s 로 단축. ~700 jsonl ×
~200 turn 매번 재 parsing 하던 비용을 sqlite 인덱스로 사전 정리.

증분 갱신 룰:
- 각 jsonl 의 mtime_ns 를 jsonl_state 테이블에 기록
- mtime 이 바뀌었거나 미기록 → load_turns 재실행 → 해당 path 의 기존 turns 삭제 + 새로 insert
- jsonl 파일 삭제 케이스는 처리 안 함 (운영 jsonl 는 누적만)

opt-in: self_eval 이 --use-cache 플래그 줄 때만 사용. 기존 직접 parsing
경로는 그대로 살아있음 (롤백 경로).
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

# self_eval 의 load_turns / iter_session_jsonl_paths 재사용
sys.path.insert(0, str(Path(__file__).parent))
from self_eval import (  # noqa: E402
    DEFAULT_PROJECTS_ROOT,
    iter_session_jsonl_paths,
    load_turns,
)

CACHE_DB = Path("/Users/yonghaekim/.claude/mindvault-v2/turns_cache.db")
DEBUG_LOG = Path("/Users/yonghaekim/.claude/mindvault-v2/debug.log")


def _debug(msg: str) -> None:
    try:
        DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DEBUG_LOG.open("a") as f:
            f.write(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] turns_cache: {msg}\n"
            )
    except Exception:
        pass


_SCHEMA = """
CREATE TABLE IF NOT EXISTS jsonl_state (
    jsonl_path TEXT PRIMARY KEY,
    mtime_ns INTEGER NOT NULL,
    last_indexed_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS turns (
    jsonl_path TEXT NOT NULL,
    ts_unix REAL NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL,
    tool_uses TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_turns_ts ON turns(ts_unix);
CREATE INDEX IF NOT EXISTS idx_turns_path ON turns(jsonl_path);
"""


def open_cache(db_path: Path = CACHE_DB) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def refresh_cache(
    projects_root: Path = DEFAULT_PROJECTS_ROOT,
    db_path: Path = CACHE_DB,
    full: bool = False,
) -> dict:
    """jsonl mtime 변경분만 재 parse, 또는 full=True 면 전체 rebuild.

    반환: {scanned, reindexed, skipped, elapsed_ms}
    """
    t0 = time.time()
    scanned = 0
    reindexed = 0
    skipped = 0
    conn = open_cache(db_path)
    try:
        if full:
            conn.execute("DELETE FROM turns")
            conn.execute("DELETE FROM jsonl_state")
            conn.commit()
        for jp in iter_session_jsonl_paths(projects_root):
            scanned += 1
            try:
                mtime_ns = jp.stat().st_mtime_ns
            except OSError:
                skipped += 1
                continue
            row = conn.execute(
                "SELECT mtime_ns FROM jsonl_state WHERE jsonl_path=?",
                (str(jp),),
            ).fetchone()
            if row and row["mtime_ns"] == mtime_ns and not full:
                skipped += 1
                continue
            # 해당 path 의 기존 turns 모두 삭제 후 재 insert
            turns = load_turns(jp)
            conn.execute(
                "DELETE FROM turns WHERE jsonl_path=?", (str(jp),)
            )
            if turns:
                conn.executemany(
                    "INSERT INTO turns(jsonl_path, ts_unix, role, text, tool_uses) "
                    "VALUES (?,?,?,?,?)",
                    [
                        (
                            str(jp),
                            t["ts_unix"],
                            t["role"],
                            t["text"],
                            json.dumps(t.get("tool_uses") or []),
                        )
                        for t in turns
                    ],
                )
            conn.execute(
                "INSERT OR REPLACE INTO jsonl_state(jsonl_path, mtime_ns, last_indexed_at) "
                "VALUES (?,?,?)",
                (str(jp), mtime_ns, time.time()),
            )
            conn.commit()
            reindexed += 1
        elapsed_ms = int((time.time() - t0) * 1000)
        _debug(
            f"refresh scanned={scanned} reindexed={reindexed} skipped={skipped} "
            f"elapsed_ms={elapsed_ms}"
        )
        return {
            "scanned": scanned,
            "reindexed": reindexed,
            "skipped": skipped,
            "elapsed_ms": elapsed_ms,
        }
    finally:
        conn.close()


def get_turns_since(
    since_unix: float,
    projects_root: Path = DEFAULT_PROJECTS_ROOT,
    db_path: Path = CACHE_DB,
    auto_refresh: bool = True,
) -> list[dict]:
    """since_unix 이후의 모든 turn (시간 정렬). 캐시 자동 갱신 옵션.

    auto_refresh=False 면 현재 캐시 내용만 조회 (테스트·디버그 용도).
    """
    if auto_refresh:
        refresh_cache(projects_root, db_path)
    conn = open_cache(db_path)
    try:
        rows = conn.execute(
            "SELECT ts_unix, role, text, tool_uses FROM turns "
            "WHERE ts_unix >= ? ORDER BY ts_unix",
            (since_unix,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "ts_unix": r["ts_unix"],
            "role": r["role"],
            "text": r["text"],
            "tool_uses": json.loads(r["tool_uses"] or "[]"),
        }
        for r in rows
    ]


def cache_stats(db_path: Path = CACHE_DB) -> dict:
    """캐시 인벤토리 진단 — jsonl 수, turn 수, DB 크기."""
    if not db_path.is_file():
        return {"exists": False}
    conn = open_cache(db_path)
    try:
        n_jsonl = conn.execute(
            "SELECT COUNT(*) FROM jsonl_state"
        ).fetchone()[0]
        n_turns = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    finally:
        conn.close()
    return {
        "exists": True,
        "db_path": str(db_path),
        "db_size_bytes": db_path.stat().st_size,
        "indexed_jsonl_files": n_jsonl,
        "indexed_turns": n_turns,
    }


def main() -> int:
    """CLI: refresh / stats / rebuild."""
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "cmd", choices=("refresh", "rebuild", "stats"), default="stats", nargs="?"
    )
    args = parser.parse_args()
    if args.cmd == "rebuild":
        out = refresh_cache(full=True)
    elif args.cmd == "refresh":
        out = refresh_cache()
    else:
        out = cache_stats()
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
