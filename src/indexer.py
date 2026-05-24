#!/usr/bin/env python3
"""MindVault v3 Sprint 2 — FTS5 증분 인덱서.

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

PROJECTS_ROOT = Path("~/.claude/projects").expanduser()
# Sprint 6: 멀티 디렉토리 스캔. Claude Code 가 cwd 마다 별도 projects 슬롯을
# 자동 생성하므로 (예: cwd=`/Users/<user>` → `-Users-<user>`, cwd=`/Users/<user>/foo`
# → `-Users-<user>-foo`) 모든 하위 디렉토리의 *.jsonl 을 흡수한다. 빈 디렉토리는
# 자연스럽게 skip. 하위 호환: PROJECTS_DIR 그대로 import 하는 코드를 위해 현재
# $HOME 기반 default 유지.
import os as _os_default
def _default_projects_dir() -> Path:
    override = _os_default.environ.get("MV3_PROJECTS_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    home_slug = "-" + str(Path.home()).strip("/").replace("/", "-")
    return PROJECTS_ROOT / home_slug


PROJECTS_DIR = _default_projects_dir()


def iter_jsonl_paths(root: Path = PROJECTS_ROOT):
    """root 하위 모든 디렉토리에서 *.jsonl yield. mtime-stable 순서."""
    if not root.is_dir():
        return
    for p in root.glob("*/*.jsonl"):
        yield p
DATA_DIR = Path("~/.claude/mindvault-v3").expanduser()
DB_PATH = DATA_DIR / "index.db"
DEBUG_LOG = DATA_DIR / "debug.log"
SIGNATURE = "# 지난 세션 요약 (MindVault v3)"
SCHEMA_VERSION = 3
# Sprint 6: 임베딩은 첫 N turn(user/assistant) 기준. 세션 의도는 앞쪽에 몰리고
# 잡담 세션은 첫 N turn도 짧고 약하므로 신호/노이즈가 자연스럽게 분리된다.
# SESSION_EMBED_CHARS는 안전망 — 한 turn에 거대한 paste가 들어와도 폭주 방지.
HEAD_TURNS_EMBED = 4
SESSION_EMBED_CHARS = 2_000

SECRET_PATTERNS = [
    (re.compile(r"sk-[a-zA-Z0-9_-]{20,}"), "[REDACTED_KEY]"),
    (re.compile(r"ghp_[a-zA-Z0-9]{20,}"), "[REDACTED_KEY]"),
    (re.compile(r"Bearer\s+[a-zA-Z0-9._-]{20,}"), "Bearer [REDACTED]"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED_AWS]"),
]

# Sprint 6: 임베딩 노이즈 제거용 메타 prefix. 첫 turn에 caveat/CLI stdout 등이
# 본문 신호를 묻어서 잡담 매칭이 일어나는 현상 차단. FTS body에는 영향 X
# (extract_head_turns_body에만 적용 — turn-단위 정제).
META_BLOCK_PATTERNS = [
    re.compile(
        r"<local-command-(caveat|stdout|stderr)>.*?</local-command-\1>",
        re.DOTALL,
    ),
    re.compile(
        r"<command-(name|message|args|stdout|stderr)>.*?</command-\1>",
        re.DOTALL,
    ),
    re.compile(r"^\[텔레그램 수신 메시지\]\s*", re.MULTILINE),
    re.compile(r"^Caveat: The messages below.*$", re.MULTILINE),
    re.compile(r"^Catch you later!\s*$", re.MULTILINE),
]


def _strip_meta_prefixes(text: str) -> str:
    """임베딩용 메타 블록·prefix 제거. 빈 줄 압축."""
    for pat in META_BLOCK_PATTERNS:
        text = pat.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


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


def extract_head_turns_body(
    jsonl_path: Path, n_turns: int = HEAD_TURNS_EMBED
) -> str:
    """첫 N개 user/assistant turn만 concat (임베딩 전용).

    세션 의도가 앞쪽에 몰리는 패턴 활용. 잡담 세션은 첫 N turn이 짧고 약해
    raw cosine이 자연스럽게 낮아진다. _is_system_reminder / SIGNATURE 필터링은
    extract_full_body와 동일하게 적용 (hook 주입 텍스트가 첫 turn에 끼는 케이스).
    """
    parts: list[str] = []
    turns = 0
    try:
        with jsonl_path.open() as f:
            for line in f:
                if turns >= n_turns:
                    break
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
                text = _strip_meta_prefixes(redact(text))
                if not text:
                    # meta-only turn (caveat 또는 stdout만 있는 경우) → 카운트 안 함
                    continue
                prefix = "U:" if t == "user" else "A:"
                parts.append(f"{prefix} {text}")
                turns += 1
    except OSError as e:
        _debug(f"head-turns read fail {jsonl_path.name}: {e}")
        return ""
    return "\n".join(parts)


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
        CREATE TABLE IF NOT EXISTS sessions_vec (
            session_id TEXT PRIMARY KEY,
            embedding BLOB NOT NULL,
            indexed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS embed_cache (
            query_hash TEXT PRIMARY KEY,
            vector BLOB NOT NULL,
            created_at TEXT NOT NULL
        );
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


def _embed_session_from_path(jsonl_path: Path) -> bytes | None:
    """jsonl 파일에서 첫 HEAD_TURNS_EMBED turn 추출 → BGE-M3 임베딩 → float32 bytes.

    SESSION_EMBED_CHARS는 한 turn에 거대 paste가 들어왔을 때 폭주 방지용 안전망.
    memory_indexer.embed_text 재사용 (BGE-M3 서버 호출 + dim 검증).
    """
    head_body = extract_head_turns_body(jsonl_path)
    text = head_body[:SESSION_EMBED_CHARS].strip()
    if not text:
        return None
    # 지연 import — sessions-only 인덱서가 BGE-M3 서버 없이도 동작하도록
    from memory_indexer import embed_text, _vec_to_blob  # noqa: WPS433
    vec = embed_text(text)
    if vec is None:
        return None
    return _vec_to_blob(vec)


# Sprint 10: 매 session 처리 후 conn.commit() — long-running write transaction을
# 짧은 트랜잭션 묶음으로 쪼개 동시 실행되는 hook의 write 대기 시간 최소화.
# 추가로 embed_text(sub-conn embed_cache write 호출)와 메인 conn write의 인터리빙을
# 분리: 매 iter 시작 시 메인이 idle → sub-conn cache_put이 BUSY 없이 진행.
# WAL 모드라 commit 부하는 무시 가능 (수백 session 인덱싱 12s 수준).


def incremental_index(
    projects_root: Path = PROJECTS_ROOT,
    db_path: Path = DB_PATH,
) -> int:
    if not projects_root.is_dir():
        _debug(f"projects root missing: {projects_root}")
        return 0
    conn = open_db(db_path)
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    updated = 0
    vec_updated = 0
    try:
        existing: dict[str, tuple[int, int]] = {}
        for row in conn.execute(
            "SELECT session_id, mtime_ns, size_bytes FROM sessions"
        ):
            existing[row["session_id"]] = (row["mtime_ns"], row["size_bytes"])

        for jsonl in iter_jsonl_paths(projects_root):
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
            # Sprint 10: 임베딩(sub-conn embed_cache write 포함)을 메인 conn write보다
            # 먼저 수행. 이전 commit 후 메인 conn이 idle인 동안 sub-conn이 충돌 없이
            # cache_put 가능 → "embed cache put fail: database is locked" 해소.
            blob = _embed_session_from_path(jsonl)
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
            if blob is not None:
                conn.execute(
                    """
                    INSERT INTO sessions_vec(session_id, embedding, indexed_at)
                    VALUES(?,?,?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        embedding=excluded.embedding,
                        indexed_at=excluded.indexed_at
                    """,
                    (sid, blob, now),
                )
                vec_updated += 1
            updated += 1
            # 매 1건 처리 후 commit — 메인 트랜잭션을 즉시 풀어 다음 iter의 임베딩
            # (sub-conn cache write 포함)과 hook write가 BUSY 없이 진행. WAL이라 부하 미미.
            conn.commit()
        conn.commit()
    finally:
        conn.close()
    if vec_updated:
        _debug(f"vec updated: {vec_updated}")
    return updated


def backfill_session_vecs(db_path: Path = DB_PATH) -> dict[str, int]:
    """sessions에는 있는데 sessions_vec에는 없는 session_id를 일괄 임베딩.

    Sprint 5 1회성 마이그레이션 + 향후 BGE-M3 다운 중 누락된 vec 복구용.
    반환: {"queued", "embedded", "failed"}.
    """
    counts = {"queued": 0, "embedded": 0, "failed": 0}
    if not db_path.is_file():
        return counts
    conn = open_db(db_path)
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        missing_rows = list(conn.execute(
            """
            SELECT s.session_id, s.file_path
            FROM sessions s
            LEFT JOIN sessions_vec v USING(session_id)
            WHERE v.session_id IS NULL
            """
        ))
        counts["queued"] = len(missing_rows)
        if not missing_rows:
            return counts
        _debug(f"backfill queued: {len(missing_rows)} sessions")
        for r in missing_rows:
            sid = r["session_id"]
            jsonl_path = Path(r["file_path"])
            if not jsonl_path.is_file():
                counts["failed"] += 1
                continue
            # Sprint 10: 임베딩(sub-conn embed_cache write 포함)을 메인 INSERT 전에 수행.
            # 매 iter 끝에 commit → 다음 iter 시작 시 메인 idle → cache_put BUSY 회피.
            blob = _embed_session_from_path(jsonl_path)
            if blob is None:
                counts["failed"] += 1
                continue
            conn.execute(
                """
                INSERT INTO sessions_vec(session_id, embedding, indexed_at)
                VALUES(?,?,?)
                ON CONFLICT(session_id) DO UPDATE SET
                    embedding=excluded.embedding,
                    indexed_at=excluded.indexed_at
                """,
                (sid, blob, now),
            )
            counts["embedded"] += 1
            conn.commit()
            if counts["embedded"] % 20 == 0:
                _debug(f"backfill progress: {counts}")
    finally:
        conn.close()
    _debug(f"backfill done: {counts}")
    return counts


def rebuild_session_vecs(db_path: Path = DB_PATH) -> dict[str, int]:
    """sessions_vec 전체 truncate 후 새 전략으로 재구축.

    Sprint 6 1회성 마이그레이션 — 기존 16K head char 임베딩과 새 head-N-turn
    임베딩은 의미가 달라서 부분 백필로는 부족. sessions / sessions_fts는 그대로.
    """
    if not db_path.is_file():
        return {"queued": 0, "embedded": 0, "failed": 0}
    conn = open_db(db_path)
    try:
        conn.execute("DELETE FROM sessions_vec")
        conn.commit()
        _debug("sessions_vec truncated for rebuild")
    finally:
        conn.close()
    return backfill_session_vecs(db_path)


def full_rebuild(
    projects_root: Path = PROJECTS_ROOT, db_path: Path = DB_PATH
) -> int:
    """sessions_* 데이터만 비우고 재인덱싱 (memories_*, embed_cache 보존).

    Sprint 10: 이전 구현은 db_path.unlink() — DB 통째 삭제 → memories_* 까지 동반 손실.
    Sprint 9 임베딩 모델 swap 중 메모리 인덱스가 함께 날아간 사고의 직접 원인이라 제거.
    rebuild_session_vecs와 일관된 패턴: 영향 테이블만 명시적으로 DELETE.

    embed_cache는 의도적으로 보존. 모델 교체로 캐시 무효화가 필요하면
    별도 호출 (e.g., `DELETE FROM embed_cache`) 또는 향후 truncate_embed_cache() 함수.
    """
    conn = open_db(db_path)
    try:
        conn.execute("DELETE FROM sessions")
        conn.execute("DELETE FROM sessions_fts")
        conn.execute("DELETE FROM sessions_vec")
        conn.commit()
        _debug("sessions_* truncated for full rebuild (memories_* preserved)")
    finally:
        conn.close()
    return incremental_index(projects_root, db_path)


def main() -> int:
    t0 = time.time()
    try:
        n = incremental_index()
        _debug(f"updated {n} sessions in {time.time() - t0:.2f}s")
        # Sprint 5: incremental 이후 vec 누락분 백필 (1회성 마이그레이션 + 누락 복구)
        bf = backfill_session_vecs()
        if bf["queued"]:
            _debug(f"backfill: {bf} (elapsed_total={time.time()-t0:.1f}s)")
    except Exception as e:
        _debug(f"FATAL {e}\n{traceback.format_exc()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
