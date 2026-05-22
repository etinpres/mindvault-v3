#!/usr/bin/env python3
"""MindVault v2 Sprint 2 — FTS5 증분 인덱서.

JSONL 세션 로그를 SQLite FTS5에 인덱싱한다. mtime + size 변경된 파일만 upsert.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
import traceback
from pathlib import Path

PROJECTS_DIR = Path("/Users/yonghaekim/.claude/projects/-Users-yonghaekim-my-folder")
DATA_DIR = Path("/Users/yonghaekim/.claude/mindvault-v2")
DB_PATH = DATA_DIR / "index.db"
DEBUG_LOG = DATA_DIR / "debug.log"
SIGNATURE = "# 지난 세션 요약 (MindVault v2)"
SCHEMA_VERSION = 2

SECRET_PATTERNS = [
    (re.compile(r"sk-[a-zA-Z0-9_-]{20,}"), "[REDACTED_KEY]"),
    (re.compile(r"ghp_[a-zA-Z0-9]{20,}"), "[REDACTED_KEY]"),
    (re.compile(r"Bearer\s+[a-zA-Z0-9._-]{20,}"), "Bearer [REDACTED]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED_AWS]"),
]


def _debug(msg: str) -> None:
    try:
        DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DEBUG_LOG.open("a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] indexer: {msg}\n")
    except Exception:
        pass


def redact(text: str) -> str:
    for pat, repl in SECRET_PATTERNS:
        text = pat.sub(repl, text)
    return text


def _is_system_reminder(text: str) -> bool:
    head = text.lstrip()[:50]
    return head.startswith("<system-reminder>") or head.startswith("<command-")


def extract_text_from_content(content) -> str:
    if isinstance(content, str):
        return "" if _is_system_reminder(content) else content
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        text_val = block.get("text")
        if btype == "text" or (btype is None and text_val is not None):
            t = str(text_val or "")
            if _is_system_reminder(t):
                continue
            parts.append(t)
    return "\n".join(p for p in parts if p)


def extract_full_body(jsonl_path: Path) -> tuple[str, str | None, str | None, int]:
    """전체 user+assistant 본문 concat. head/tail 제한 없음 (인덱싱은 완전 커버)."""
    parts: list[str] = []
    first_ts: str | None = None
    last_ts: str | None = None
    turns = 0
    try:
        with jsonl_path.open() as f:
            for line in f:
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = d.get("type")
                if t not in ("user", "assistant"):
                    continue
                msg = d.get("message") or {}
                content = msg.get("content")
                text = extract_text_from_content(content).strip()
                if not text or SIGNATURE in text:
                    continue
                text = redact(text)
                ts = d.get("timestamp") or ""
                if first_ts is None and ts:
                    first_ts = ts
                if ts:
                    last_ts = ts
                prefix = "U:" if t == "user" else "A:"
                parts.append(f"{prefix} {text}")
                turns += 1
    except OSError as e:
        _debug(f"read fail {jsonl_path.name}: {e}")
        return "", None, None, 0
    return "\n".join(parts), first_ts, last_ts, turns


def _init_db(conn: sqlite3.Connection) -> None:
    # NOTE: sqlite-vec(vec0) virtual table은 macOS 시스템 Python의 sqlite3가
    # `enable_load_extension`을 지원하지 않아 사용 불가. 대신 일반 BLOB 컬럼에
    # float32 numpy bytes로 저장하고, memory_search.py에서 Python cosine 계산.
    # 메모리 자산이 ~100개 규모라 인덱스 검색의 O(log n) 이점이 무의미.
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS sessions (
            session_id TEXT PRIMARY KEY,
            file_path TEXT NOT NULL,
            mtime_ns INTEGER NOT NULL,
            size_bytes INTEGER NOT NULL,
            first_ts TEXT,
            last_ts TEXT,
            turn_count INTEGER,
            indexed_at TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
            session_id UNINDEXED,
            body,
            tokenize = 'unicode61 remove_diacritics 2'
        );
        CREATE TABLE IF NOT EXISTS memories (
            path TEXT PRIMARY KEY,
            name TEXT,
            description TEXT,
            mtime_ns INTEGER NOT NULL,
            indexed_at TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
            path UNINDEXED,
            body,
            tokenize = 'unicode61 remove_diacritics 2'
        );
        CREATE TABLE IF NOT EXISTS memories_vec (
            rowid INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT NOT NULL,
            kind TEXT NOT NULL,
            embedding BLOB NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_memories_vec_path ON memories_vec(path);
        """
    )
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def open_db(db_path: Path = DB_PATH) -> sqlite3.Connection:
    """DB 열고 스키마 보장. V1→V2 마이그레이션은 sessions 보존 (ALTER/CREATE IF NOT EXISTS only)."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # corrupt DB 점검 — DatabaseError 시에만 unlink (스키마 mismatch는 ALTER로 보존)
    try:
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
    except sqlite3.DatabaseError:
        conn.close()
        try:
            db_path.unlink()
        except FileNotFoundError:
            pass
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
    _init_db(conn)
    return conn


def incremental_index(
    projects_dir: Path = PROJECTS_DIR,
    db_path: Path = DB_PATH,
) -> int:
    if not projects_dir.is_dir():
        _debug(f"projects dir missing: {projects_dir}")
        return 0
    conn = open_db(db_path)
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    updated = 0
    try:
        existing: dict[str, tuple[int, int]] = {}
        for row in conn.execute(
            "SELECT session_id, mtime_ns, size_bytes FROM sessions"
        ):
            existing[row["session_id"]] = (row["mtime_ns"], row["size_bytes"])

        for jsonl in projects_dir.glob("*.jsonl"):
            sid = jsonl.stem
            try:
                st = jsonl.stat()
            except OSError:
                continue
            if existing.get(sid) == (st.st_mtime_ns, st.st_size):
                continue
            body, first_ts, last_ts, turns = extract_full_body(jsonl)
            if not body:
                continue
            conn.execute(
                """
                INSERT INTO sessions(session_id, file_path, mtime_ns, size_bytes,
                                     first_ts, last_ts, turn_count, indexed_at)
                VALUES(?,?,?,?,?,?,?,?)
                ON CONFLICT(session_id) DO UPDATE SET
                    file_path=excluded.file_path,
                    mtime_ns=excluded.mtime_ns,
                    size_bytes=excluded.size_bytes,
                    first_ts=excluded.first_ts,
                    last_ts=excluded.last_ts,
                    turn_count=excluded.turn_count,
                    indexed_at=excluded.indexed_at
                """,
                (sid, str(jsonl), st.st_mtime_ns, st.st_size,
                 first_ts, last_ts, turns, now),
            )
            conn.execute("DELETE FROM sessions_fts WHERE session_id=?", (sid,))
            conn.execute(
                "INSERT INTO sessions_fts(session_id, body) VALUES(?,?)",
                (sid, body),
            )
            updated += 1
        conn.commit()
    finally:
        conn.close()
    return updated


def full_rebuild(
    projects_dir: Path = PROJECTS_DIR, db_path: Path = DB_PATH
) -> int:
    try:
        db_path.unlink()
    except FileNotFoundError:
        pass
    return incremental_index(projects_dir, db_path)


def main() -> int:
    t0 = time.time()
    try:
        n = incremental_index()
        _debug(f"updated {n} sessions in {time.time() - t0:.2f}s")
    except Exception as e:
        _debug(f"FATAL {e}\n{traceback.format_exc()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
