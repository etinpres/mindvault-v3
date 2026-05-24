"""MindVault v3 NEXT-16 — extractor prompt → candidates 결과 캐시.

같은 prompt SHA256 hash → 캐시 hit. Gemma 안 호출, 즉시 결과 반환.
jsonl 이 변하면 prompt 가 달라져 hash 도 달라지므로 자동 invalidate.

캐시 위치: ~/.claude/mindvault-v2/extractor_cache.db (sqlite)
opt-out: MV2_EXTRACTOR_CACHE_DISABLE=1
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

CACHE_DB = Path("/Users/yonghaekim/.claude/mindvault-v2/extractor_cache.db")
CACHE_DISABLE_ENV = "MV2_EXTRACTOR_CACHE_DISABLE"

_init_lock = threading.Lock()
_initialized = False


def cache_enabled() -> bool:
    return os.environ.get(CACHE_DISABLE_ENV, "0") != "1"


def _ensure_db() -> None:
    """idempotent schema init. WAL mode 로 hook 동시 호출 충돌 회피."""
    global _initialized
    if _initialized:
        return
    with _init_lock:
        if _initialized:
            return
        CACHE_DB.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(CACHE_DB), timeout=5.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS extractor_cache (
                    prompt_hash TEXT PRIMARY KEY,
                    candidates_json TEXT NOT NULL,
                    candidate_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    last_hit_at TEXT,
                    hit_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.commit()
        finally:
            conn.close()
        _initialized = True


def prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def cache_get(prompt: str) -> Optional[list[dict]]:
    """hit → list[dict] (즉시 hit_count++). miss → None."""
    if not cache_enabled():
        return None
    _ensure_db()
    h = prompt_hash(prompt)
    conn = sqlite3.connect(str(CACHE_DB), timeout=5.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        row = conn.execute(
            "SELECT candidates_json FROM extractor_cache WHERE prompt_hash=?",
            (h,),
        ).fetchone()
        if not row:
            return None
        # hit 카운터 갱신 (best-effort, 실패해도 무시)
        try:
            conn.execute(
                "UPDATE extractor_cache SET hit_count=hit_count+1, "
                "last_hit_at=? WHERE prompt_hash=?",
                (_now(), h),
            )
            conn.commit()
        except sqlite3.Error:
            pass
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return None
    finally:
        conn.close()


def cache_put(prompt: str, candidates: list[dict]) -> None:
    """결과 저장. 빈 list 도 저장 — 다음 호출에서 같은 prompt 면 Gemma 재시도 피함."""
    if not cache_enabled():
        return
    _ensure_db()
    h = prompt_hash(prompt)
    conn = sqlite3.connect(str(CACHE_DB), timeout=5.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            INSERT OR REPLACE INTO extractor_cache
            (prompt_hash, candidates_json, candidate_count, created_at, hit_count)
            VALUES (?, ?, ?, ?, COALESCE(
                (SELECT hit_count FROM extractor_cache WHERE prompt_hash=?), 0
            ))
            """,
            (h, json.dumps(candidates, ensure_ascii=False), len(candidates),
             _now(), h),
        )
        conn.commit()
    finally:
        conn.close()


def cache_stats() -> dict:
    """관측용: total entries, total hits, hit rate, top entries."""
    _ensure_db()
    conn = sqlite3.connect(str(CACHE_DB), timeout=5.0)
    try:
        total = conn.execute(
            "SELECT COUNT(*), SUM(hit_count), SUM(candidate_count) "
            "FROM extractor_cache"
        ).fetchone()
        return {
            "entries": total[0] or 0,
            "total_hits": total[1] or 0,
            "total_candidates": total[2] or 0,
        }
    finally:
        conn.close()


def cache_clear() -> int:
    """캐시 전체 삭제. 반환: 삭제 row 수. 테스트·운영 invalidation 용."""
    _ensure_db()
    conn = sqlite3.connect(str(CACHE_DB), timeout=5.0)
    try:
        cur = conn.execute("DELETE FROM extractor_cache")
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")
