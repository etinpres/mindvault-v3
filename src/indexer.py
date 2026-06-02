#!/usr/bin/env python3
"""MindVault v3 Sprint 2 — FTS5 증분 인덱서.

JSONL 세션 로그를 SQLite FTS5에 인덱싱한다. mtime + size 변경된 파일만 upsert.
"""
from __future__ import annotations

import fcntl
import json
import re
import sqlite3
import sys
import time
import traceback
from pathlib import Path

# v3.2.7: production state pollution 방지. MV3_PROJECTS_ROOT env var 우선.
import os as _os_paths
PROJECTS_ROOT = Path(_os_paths.environ.get("MV3_PROJECTS_ROOT", "~/.claude/projects")).expanduser()
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
DATA_DIR = Path(_os_paths.environ.get("MV3_DATA_DIR", "~/.claude/mindvault-v3")).expanduser()
DB_PATH = DATA_DIR / "index.db"
DEBUG_LOG = DATA_DIR / "debug.log"
# bug-audit 2026-05-29 (indexing-session-indexer-no-lock-1): 세션 인덱서는
# trigger_background_indexer 가 락 없이 detach spawn 하므로 동시 실행 가능했다.
# WAL 로 동시 write 가 안전해졌어도 두 인덱서가 같은 jsonl 을 중복 스캔·임베딩하는
# 낭비가 남는다. memory-indexer.lock 과 동형의 flock(LOCK_NB)으로 직렬화 (별도
# 락 파일이라 memory 인덱서와는 독립).
SESSION_LOCK_PATH = DATA_DIR / "session-indexer.lock"
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
    _migrate_schema(conn)
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """schema_version → SCHEMA_VERSION 까지 차분 ALTER 적용.

    audit-2026-05-24: 기존엔 `CREATE TABLE IF NOT EXISTS` 만으로 idempotent
    init 했으나 컬럼 추가가 들어가면 silent skip. 빈 골격을 두고 향후
    sprint 에서 마이그레이션 step 만 추가하면 되도록.

    NOTE: 새 step 추가 시 (1) `SCHEMA_VERSION` 상수도 함께 증가, (2) step
    안에서 `current = N` 갱신, (3) 멱등성 보장(`IF NOT EXISTS`/`OR IGNORE`).
    SCHEMA_VERSION 증가를 잊으면 다음 호출에서 early-out 으로 silent skip.
    """
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
    except sqlite3.DatabaseError:
        row = None
    try:
        current = int(row[0]) if row and row[0] else 0
    except (TypeError, ValueError):
        current = 0
    # current → SCHEMA_VERSION 차분 적용. 비어 있어도 OK — 첫 install 또는
    # 이미 최신. step 추가 시 `if current < N: conn.execute(...); current = N`.
    if current >= SCHEMA_VERSION:
        return
    # 예시 step (향후 컬럼 추가 시):
    #   if current < 4:
    #       conn.execute("ALTER TABLE memories ADD COLUMN tags TEXT")
    #       current = 4
    return


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
    # bug-audit 2026-05-29 (indexing-wal-missing-1): WAL + busy_timeout 설정.
    # 이전엔 둘 다 미설정이라 index.db 가 기본 journal_mode=DELETE 로 동작 →
    # 세션 인덱서 / 메모리 인덱서 / embed_cache sub-conn / recall hot path 가
    # 같은 파일을 동시에 열 때 SQLITE_BUSY 와 'unable to open database file'
    # (운영 370건+) 빈발. WAL 은 reader 가 writer 를 막지 않게 하고 busy_timeout
    # 은 짧은 쓰기 락 대기를 흡수한다. extractor_cache.py 의 검증된 패턴과 동일.
    # WAL 은 DB파일 1회 전환 후 영속, busy_timeout 은 conn 마다 재설정 필요하므로
    # 모든 open_db 호출에서 설정한다.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
    except sqlite3.DatabaseError:
        pass  # 네트워크 FS 등 WAL 미지원 환경 — 기존 동작으로 폴백
    _init_db(conn)
    return conn


class EmbedUnavailable(Exception):
    """임베딩 서버 일시 장애 (timeout/connection refused) — 재시도 대상.

    bug-audit 2026-05-29 (indexing-embed-fail-permanent-sentinel-1): 이전엔
    '본문 없음'(영구 skip 정당)과 '임베딩 서버 다운'(일시적, 재시도해야 함)이
    모두 None 으로 합쳐져, backfill 이 서버 다운 중에도 영구 빈-blob sentinel 을
    박아 해당 세션이 서버 복구 후에도 semantic recall 에서 영구 제외됐다.
    이 예외로 두 경우를 분리: 본문 없음은 None 반환(영구 sentinel), 서버 장애는
    예외 발생(sentinel 미기록 → 다음 backfill 재시도).
    """


def _embed_session_from_path(jsonl_path: Path) -> bytes | None:
    """jsonl 파일에서 첫 HEAD_TURNS_EMBED turn 추출 → 임베딩(Arctic-ko) → float32 bytes.

    SESSION_EMBED_CHARS는 한 turn에 거대 paste가 들어왔을 때 폭주 방지용 안전망.
    memory_indexer.embed_text 재사용 (임베딩 서버(Arctic-ko) 호출 + dim 검증).

    Returns:
        bytes: 정상 임베딩.
        None: 본문이 비어 임베딩 대상이 아님 (영구 skip — sentinel 정당).
    Raises:
        EmbedUnavailable: 임베딩 서버 일시 장애 (재시도 대상 — sentinel 금지).
    """
    head_body = extract_head_turns_body(jsonl_path)
    text = head_body[:SESSION_EMBED_CHARS].strip()
    if not text:
        return None
    # 지연 import — sessions-only 인덱서가 임베딩 서버 없이도 동작하도록
    from memory_indexer import embed_text, _vec_to_blob  # noqa: WPS433
    vec = embed_text(text)
    if vec is None:
        raise EmbedUnavailable(jsonl_path.name)
    return _vec_to_blob(vec)


# Sprint 10: 매 session 처리 후 conn.commit() — long-running write transaction을
# 짧은 트랜잭션 묶음으로 쪼개 동시 실행되는 hook의 write 대기 시간 최소화.
# 추가로 embed_text(sub-conn embed_cache write 호출)와 메인 conn write의 인터리빙을
# 분리: 매 iter 시작 시 메인이 idle → sub-conn cache_put이 BUSY 없이 진행.
# WAL 모드라 commit 부하는 무시 가능 (수백 session 인덱싱 12s 수준).


def _session_lock_path(db_path: Path) -> Path:
    """db_path 페어 lock — production 은 DATA_DIR/session-indexer.lock, 테스트는
    tmp_db 페어로 분리해 production 오염 차단 (memory_indexer._lock_path_for 동형)."""
    if Path(db_path).resolve() == DB_PATH.resolve():
        return SESSION_LOCK_PATH
    return Path(db_path).parent / "session-indexer.lock"


def _acquire_session_lock(db_path: Path):
    """flock(LOCK_NB) — 동시 세션 인덱서 차단. 못 잡으면 None."""
    lock_path = _session_lock_path(db_path)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = lock_path.open("w")
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except (BlockingIOError, OSError):
        try:
            fh.close()
        except (OSError, NameError, UnboundLocalError):
            pass
        return None


def _release_session_lock(fh) -> None:
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


def incremental_index(
    projects_root: Path = PROJECTS_ROOT,
    db_path: Path = DB_PATH,
) -> int:
    if not projects_root.is_dir():
        _debug(f"projects root missing: {projects_root}")
        return 0
    lock = _acquire_session_lock(db_path)
    if lock is None:
        _debug("session-indexer lock busy — skip")
        return 0
    try:
        conn = open_db(db_path)
    except Exception:
        _release_session_lock(lock)  # open_db 실패 시 락 누수 방지
        raise
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
            # bug-audit 2026-05-29: 임베딩 서버 일시 장애 시엔 FTS 만 인덱싱하고
            # vec 는 다음 backfill 이 채우게 한다 (영구 누락 방지).
            try:
                blob = _embed_session_from_path(jsonl)
            except EmbedUnavailable:
                blob = None
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
            else:
                # bug-audit 2026-06-01 (session-vec-stale-on-update): 임베드 실패 시 기존
                # 세션의 sessions_vec 를 그대로 두면, mtime 갱신으로 다음 incremental_index
                # 가 skip + backfill 의 'session_id IS NULL' 필터도 present-but-stale 행을
                # 못 잡아 body↔vec 가 영구 stale(옛 본문 임베딩으로 잘못된 semantic recall).
                # 행을 제거해 NULL(missing) 로 되돌려 backfill 이 서버 복구 후 재충전하게 한다.
                conn.execute("DELETE FROM sessions_vec WHERE session_id=?", (sid,))
            updated += 1
            # 매 1건 처리 후 commit — 메인 트랜잭션을 즉시 풀어 다음 iter의 임베딩
            # (sub-conn cache write 포함)과 hook write가 BUSY 없이 진행. WAL이라 부하 미미.
            conn.commit()
        conn.commit()
    finally:
        conn.close()
        _release_session_lock(lock)
    if vec_updated:
        _debug(f"vec updated: {vec_updated}")
    return updated


def backfill_session_vecs(db_path: Path = DB_PATH) -> dict[str, int]:
    """sessions에는 있는데 sessions_vec에는 없는 session_id를 일괄 임베딩.

    Sprint 5 1회성 마이그레이션 + 향후 임베딩 서버 다운 중 누락된 vec 복구용.
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
                # NEXT-28: 파일 자체가 없는 세션도 sentinel 처리 — 다음 backfill
                # 에서 같은 sid 재시도 차단. 파일이 나중에 복구되면 ON CONFLICT
                # 분기에서 새 vec로 갱신됨.
                conn.execute(
                    "INSERT OR IGNORE INTO sessions_vec(session_id, embedding, indexed_at) VALUES(?,?,?)",
                    (sid, b"", now),
                )
                conn.commit()
                continue
            # Sprint 10: 임베딩(sub-conn embed_cache write 포함)을 메인 INSERT 전에 수행.
            # 매 iter 끝에 commit → 다음 iter 시작 시 메인 idle → cache_put BUSY 회피.
            # bug-audit 2026-05-29 (indexing-embed-fail-permanent-sentinel-1):
            # 임베딩 서버 일시 장애(EmbedUnavailable)는 sentinel 을 박지 않고
            # failed 카운트만 올린 뒤 다음 backfill 에서 재시도되게 한다. 이전엔
            # 서버 다운 중 backfill 이 돌면 영구 빈-blob sentinel 이 박혀 서버
            # 복구 후에도 그 세션이 semantic recall 에서 영구 제외됐다.
            try:
                blob = _embed_session_from_path(jsonl_path)
            except EmbedUnavailable:
                counts["failed"] += 1
                continue  # sentinel 미기록 — 다음 backfill 재시도
            if blob is None:
                counts["failed"] += 1
                # NEXT-28 (2026-05-24): 본문 추출 결과가 비어있는 jsonl 세션
                # (예: last-prompt + local-command-stdout 같은 메타-only 파일)은
                # 매번 backfill 큐에 다시 잡혀 같은 7건이 무한 재시도되었음.
                # 빈 blob sentinel을 sessions_vec에 박아 LEFT JOIN IS NULL 쿼리
                # 에서 제외. 검색 측은 frombuffer shape != EMBED_DIM 분기에서
                # 자동 skip. (본문 없음은 영구 skip 이 정당 — 재시도해도 항상 빈 본문.)
                conn.execute(
                    "INSERT OR IGNORE INTO sessions_vec(session_id, embedding, indexed_at) VALUES(?,?,?)",
                    (sid, b"", now),
                )
                conn.commit()
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
