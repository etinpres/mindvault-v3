#!/usr/bin/env python3
"""MindVault v3 Sprint 4 — memory/*.md → BLOB-기반 vec + FTS5 이중 인덱서.

설계 결정:
- vec 저장은 sqlite-vec 대신 일반 BLOB 컬럼 + numpy float32 (macOS 시스템
  Python sqlite3의 enable_load_extension 미지원 회피). 메모리 ~100개 규모라
  성능 차이 무시 가능.
- 이중 임베딩: body 전체 + frontmatter description 각각. description은 정수만
  박혀있어 매칭 정밀도가 높아 검색 시 1.5x 가중.
- 변경 감지: mtime_ns 비교. 같으면 skip, 다르면 재임베딩 + DB upsert.
- 동시성: flock(LOCK_NB)로 동시 indexer 실행 차단.
- path traversal: symlink resolve 후 allowed_roots 하위 확인.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

import numpy as np
import yaml

DATA_DIR = Path("~/.claude/mindvault-v3").expanduser()
DB_PATH = DATA_DIR / "index.db"
DEBUG_LOG = DATA_DIR / "debug.log"
LOCK_PATH = DATA_DIR / "memory-indexer.lock"
EMBED_URL = "http://localhost:8081/embed"
EMBED_TIMEOUT = 5  # seconds — 인덱싱 시점은 hook과 별개라 여유
EMBED_DIM = 1024
# Sprint 9: BGE-M3 → Arctic-Embed-L v2.0 KO 교체. CLS pooling + L2 normalized.
# 서버가 "kind" 필드를 사용 (query → "query: " prefix 자동 부착).
# Claude Code 가 cwd 마다 별도 projects 슬롯을 만들기 때문에
# (예: cwd=`/Users/<user>` → `~/.claude/projects/-Users-<user>/`,
# cwd=`/Users/<user>/foo` → `~/.claude/projects/-Users-<user>-foo/`) 런타임 glob 으로
# 모든 슬롯의 memory 디렉토리를 흡수한다. 사용자가 직접 슬러그를 입력할 필요 없음.
def _discover_memory_dirs() -> list[Path]:
    root = Path("~/.claude/projects").expanduser()
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob("*/memory") if p.is_dir())


DEFAULT_MEMORY_DIRS = _discover_memory_dirs()
# Sprint 11: env var `MV3_EXTRA_MEMORY_DIRS=path1:path2` 로 추가 indexing 디렉토리.
# 예: handoff/ 폴더에 sprint 별 brief/build-log 두는 환경에서 그 콘텐츠를 회수
# 가능하게. hook의 _spawn_reindex가 부모 env 보존하므로 shell rc에 export 1회면
# indexer + hook 양쪽에 자동 적용.
ENV_EXTRA_DIRS = "MV3_EXTRA_MEMORY_DIRS"


SOURCES_CONFIG = DATA_DIR / "sources.json"
# Sprint 16: env var(MV3_EXTRA_MEMORY_DIRS) + config file(sources.json) union.
# env 는 shell session 한정, config 는 영구. sources_cli.py 로 add/remove/list.


def _config_memory_dirs() -> list[Path]:
    """sources.json 의 sources 항목 → Path 리스트. 실패 시 빈 리스트."""
    if not SOURCES_CONFIG.is_file():
        return []
    try:
        import json as _json
        data = _json.loads(SOURCES_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    srcs = data.get("sources")
    if not isinstance(srcs, list):
        return []
    out: list[Path] = []
    for s in srcs:
        if not isinstance(s, str) or not s:
            continue
        out.append(Path(s).expanduser())
    return out


def _extra_memory_dirs() -> list[Path]:
    """env var + config file dirs union (env 우선·dedup)."""
    out: list[Path] = []
    seen: set[str] = set()
    raw = os.environ.get(ENV_EXTRA_DIRS, "").strip()
    if raw:
        for piece in raw.split(":"):
            piece = piece.strip()
            if not piece:
                continue
            p = Path(piece).expanduser()
            key = str(p)
            if key not in seen:
                seen.add(key)
                out.append(p)
    for p in _config_memory_dirs():
        key = str(p)
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

# 같은 디렉토리의 indexer.py(Sprint 1~3)에서 secret 마스킹 + open_db 재사용
sys.path.insert(0, str(Path(__file__).parent))
from indexer import redact, open_db  # noqa: E402


def _debug(msg: str) -> None:
    try:
        DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DEBUG_LOG.open("a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] mem-indexer: {msg}\n")
    except Exception:
        pass


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """마크다운에서 YAML frontmatter dict + 본문 분리. 실패 시 ({}, text)."""
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
        if not isinstance(fm, dict):
            return {}, text
    except yaml.YAMLError:
        return {}, text
    return fm, text[m.end():]


def _embed_cache_get(query: str, kind: str) -> list[float] | None:
    """sqlite embed_cache 조회. cache key는 (kind, text) 묶음 — query/passage 분리.

    Sprint 10: 메인 indexer 트랜잭션이 짧아졌으므로 timeout 기본값(5s) 복구.
    이전엔 timeout=0.1로 self-deadlock 회피했으나 짧은 트랜잭션에선 BUSY 대기로 충분.
    """
    h = hashlib.sha256(f"{kind}\x00{query}".encode("utf-8")).hexdigest()
    try:
        conn = sqlite3.connect(str(DB_PATH))
        try:
            row = conn.execute(
                "SELECT vector FROM embed_cache WHERE query_hash=?", (h,)
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return None
    if not row:
        return None
    arr = np.frombuffer(row[0], dtype=np.float32)
    if arr.shape != (EMBED_DIM,):
        return None
    return arr.tolist()


def _embed_cache_put(query: str, kind: str, vector: list[float]) -> None:
    """sqlite embed_cache 저장. cache key는 (kind, text) 묶음.

    Sprint 10: timeout 기본값(5s) 복구 (위 _embed_cache_get 주석 참고).
    """
    h = hashlib.sha256(f"{kind}\x00{query}".encode("utf-8")).hexdigest()
    try:
        blob = np.asarray(vector, dtype=np.float32).tobytes()
        conn = sqlite3.connect(str(DB_PATH))
        try:
            conn.execute(
                "INSERT OR REPLACE INTO embed_cache(query_hash, vector, created_at) VALUES(?,?,?)",
                (h, blob, time.strftime("%Y-%m-%dT%H:%M:%S")),
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.DatabaseError as e:
        _debug(f"embed cache put fail: {e}")


def embed_text(text: str, kind: str = "passage") -> list[float] | None:
    """임베딩 서버 호출 → 1024차원 dense 벡터.

    kind="passage" (기본): 문서/메모리 본문 임베딩.
    kind="query": 검색 쿼리 임베딩. Arctic-Embed-L v2.0 KO 학습 설정상 서버가
    "query: " prefix를 자동 부착한다.

    Sprint 8 sqlite embed_cache 유지 — cache key는 (kind, text) 묶음이라 같은
    텍스트라도 query/passage 분리 저장. 모델 교체 시 embed_cache truncate 필요.
    """
    text = (text or "").strip()
    if not text:
        return None
    if kind not in ("query", "passage"):
        raise ValueError(f"kind must be 'query' or 'passage', got {kind!r}")
    cached = _embed_cache_get(text, kind)
    if cached is not None:
        return cached
    body = json.dumps({"input": text, "kind": kind}).encode("utf-8")
    req = urllib.request.Request(
        EMBED_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=EMBED_TIMEOUT) as resp:
            data = json.loads(resp.read())
        vec = data.get("vector")
        if not isinstance(vec, list) or len(vec) != EMBED_DIM:
            _debug(
                f"embed bad shape: type={type(vec).__name__} "
                f"len={len(vec) if isinstance(vec, list) else '?'}"
            )
            return None
        _embed_cache_put(text, kind, vec)
        return vec
    except (TimeoutError, urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        _debug(f"embed fail: {type(e).__name__}: {e}")
        return None


def _safe_memory_path(path: Path, allowed_roots: list[Path]) -> bool:
    """path가 allowed_roots 중 하나의 하위인지 (symlink resolve 포함)."""
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        return False
    for root in allowed_roots:
        try:
            resolved.relative_to(root.resolve(strict=False))
            return True
        except ValueError:
            continue
    return False


PROCEDURAL_SUBDIR = "_procedural"
# Sprint 13: 절차적 메모리(명령어 syntax·workflow·환경 설정)는 결정 메모리와
# 분리해 `_procedural/` 하위 디렉토리에 저장. 디렉토리 단위 분리로 grep·인벤토리·
# 백업이 단순해진다. 회수 시점에는 동일 게이트로 검색 — type 분리는 저장 슬롯의
# 의미만 가지고, 검색 우선순위는 raw cosine 그대로 따른다.


def _collect_md_files(dirs: list[Path]) -> list[Path]:
    """memory/ 디렉토리에서 .md 수집. _staged/, MEMORY.md(index 파일), symlink outside 제외.

    Sprint 13: root 직속 + `_procedural/` 하위까지 수집. `_procedural/_staged/` 는
    `_staged` 부분 일치로 제외.
    """
    out: list[Path] = []
    for d in dirs:
        if not d.is_dir():
            continue
        candidates: list[Path] = list(d.glob("*.md"))
        proc_dir = d / PROCEDURAL_SUBDIR
        if proc_dir.is_dir():
            candidates.extend(proc_dir.glob("*.md"))
        for p in candidates:
            if any(part == "_staged" for part in p.parts):
                continue
            if p.name == "MEMORY.md":
                # MEMORY.md는 다른 메모리들의 인덱스(목차)일 뿐. 본문이 한국어
                # 일반 키워드로 가득해 무관 쿼리에 fts hit으로 끼는 노이즈 원인.
                continue
            if not _safe_memory_path(p, dirs):
                _debug(f"unsafe path skip: {p}")
                continue
            out.append(p)
    return out


def _vec_to_blob(vec: list[float]) -> bytes:
    """list[float] → float32 little-endian bytes (numpy)."""
    return np.asarray(vec, dtype=np.float32).tobytes()


def _parse_memory_file(path: Path) -> tuple[dict, str] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        _debug(f"read fail {path}: {e}")
        return None
    fm, body = parse_frontmatter(text)
    return fm, redact(body)


def _lock_path_for(db_path: Path | None) -> Path:
    """db_path 와 같은 디렉토리에 lock 위치 — production 은 DATA_DIR/index.db
    페어로 DATA_DIR/memory-indexer.lock 유지, 테스트는 tmp_db 페어로 tmp 격리."""
    if db_path is None or Path(db_path).resolve() == DB_PATH.resolve():
        return LOCK_PATH
    return Path(db_path).parent / "memory-indexer.lock"


def _acquire_lock(db_path: Path | None = None):
    """flock(LOCK_NB) — 동시 실행 차단. lock 못 잡으면 None.
    post-ship: db_path 인자 추가 — 테스트가 tmp_db 사용 시 lock 도 tmp 로
    분리되어 production ~/.claude/mindvault-v3/ 오염 차단.
    """
    lock_path = _lock_path_for(db_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = lock_path.open("w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except BlockingIOError:
        fh.close()
        return None


def _release_lock(fh) -> None:
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    finally:
        fh.close()


# Sprint 10: 매 .md 처리 후 conn.commit() — long-running write transaction을
# 짧은 트랜잭션 묶음으로 쪼개 hook(memory-recall.py 등) 동시 실행 시 lock 충돌 회피.
# 추가로 embed_text(sub-conn embed_cache write 호출)는 메인 conn write 전에 수행하되,
# 매 iter 끝에 commit해 다음 iter 시작 시 메인 idle → sub-conn cache_put BUSY 회피.
# WAL 모드라 commit 부하 무시 (~100 memory 규모 풀 리빌드 단위 시간 미만).


def incremental_index(
    memory_dirs: list[Path] | None = None,
    db_path: Path | None = None,
) -> dict[str, int]:
    """변경된 .md만 재임베딩. 반환: {"updated", "skipped", "removed"}.

    lock 못 잡으면 즉시 0으로 반환 (다른 indexer가 작업 중).
    """
    if memory_dirs is None:
        memory_dirs = DEFAULT_MEMORY_DIRS + _extra_memory_dirs()
    if db_path is None:
        db_path = DB_PATH

    counts = {"updated": 0, "skipped": 0, "removed": 0}
    lock = _acquire_lock(db_path)
    if lock is None:
        _debug("lock busy — skip")
        return counts

    try:
        conn = open_db(db_path)
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        try:
            existing = {
                r["path"]: r["mtime_ns"]
                for r in conn.execute("SELECT path, mtime_ns FROM memories")
            }
            present_files = _collect_md_files(memory_dirs)
            present_paths = {str(p) for p in present_files}

            # 1) 삭제된 파일 정리 — 각 stale 처리 후 즉시 commit해 hook write 대기 최소화.
            for stale_path in existing.keys() - present_paths:
                conn.execute("DELETE FROM memories WHERE path=?", (stale_path,))
                conn.execute(
                    "DELETE FROM memories_fts WHERE path=?", (stale_path,)
                )
                conn.execute(
                    "DELETE FROM memories_vec WHERE path=?", (stale_path,)
                )
                counts["removed"] += 1
                conn.commit()

            # 2) 신규/변경 파일 처리
            for p in present_files:
                try:
                    st = p.stat()
                except OSError:
                    continue
                sp = str(p)
                if existing.get(sp) == st.st_mtime_ns:
                    counts["skipped"] += 1
                    continue

                parsed = _parse_memory_file(p)
                if parsed is None:
                    continue
                fm, body = parsed
                name = (fm.get("name") or p.stem)
                description = (fm.get("description") or "")

                # Sprint 10: 임베딩(sub-conn embed_cache write 포함)을 메인 conn write 전에 수행.
                # 이전 iter의 commit 직후라 메인 conn이 idle → sub-conn cache_put BUSY 회피.
                vec_body = embed_text(body) if body.strip() else None
                vec_desc = embed_text(description) if description.strip() else None

                conn.execute(
                    "DELETE FROM memories_fts WHERE path=?", (sp,)
                )
                conn.execute(
                    "DELETE FROM memories_vec WHERE path=?", (sp,)
                )
                conn.execute(
                    """
                    INSERT INTO memories(path, name, description, mtime_ns, indexed_at)
                    VALUES(?,?,?,?,?)
                    ON CONFLICT(path) DO UPDATE SET
                        name=excluded.name,
                        description=excluded.description,
                        mtime_ns=excluded.mtime_ns,
                        indexed_at=excluded.indexed_at
                    """,
                    (sp, name, description, st.st_mtime_ns, now),
                )
                conn.execute(
                    "INSERT INTO memories_fts(path, body) VALUES(?,?)",
                    (sp, body),
                )
                if vec_body is not None:
                    conn.execute(
                        "INSERT INTO memories_vec(path, kind, embedding) "
                        "VALUES(?,?,?)",
                        (sp, "body", _vec_to_blob(vec_body)),
                    )
                if vec_desc is not None:
                    conn.execute(
                        "INSERT INTO memories_vec(path, kind, embedding) "
                        "VALUES(?,?,?)",
                        (sp, "description", _vec_to_blob(vec_desc)),
                    )
                counts["updated"] += 1
                conn.commit()
        finally:
            conn.close()
    finally:
        _release_lock(lock)

    _debug(f"incremental: {counts}")
    return counts


def full_rebuild(
    memory_dirs: list[Path] | None = None,
    db_path: Path | None = None,
) -> int:
    """memories_* 데이터 비우고 재인덱싱 (sessions_* 보존)."""
    if db_path is None:
        db_path = DB_PATH
    conn = open_db(db_path)
    try:
        conn.execute("DELETE FROM memories")
        conn.execute("DELETE FROM memories_fts")
        conn.execute("DELETE FROM memories_vec")
        conn.commit()
    finally:
        conn.close()
    return incremental_index(memory_dirs, db_path)["updated"]


def main() -> int:
    t0 = time.time()
    try:
        c = incremental_index()
        _debug(f"main: {c} in {time.time()-t0:.2f}s")
    except Exception as e:
        _debug(f"FATAL: {e}\n{traceback.format_exc()}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
