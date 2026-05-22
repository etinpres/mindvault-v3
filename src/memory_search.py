#!/usr/bin/env python3
"""MindVault v2 Sprint 4 — hybrid RRF memory 검색.

알고리즘:
1. 쿼리 임베딩 (BGE-M3)
2. FTS5 BM25 top-10 (body) + Vec cosine top-10 (BLOB float32, numpy)
3. RRF 결합: score = Σ (가중 적용된 1/(60+rank)) — description 1.5x
4. min-max 정규화 (배치 내 독립) → score_threshold 게이트 → top_k

vec 저장은 BLOB(float32 bytes)이므로 전체 row 로드 후 numpy cosine.
메모리 자산이 ~100개라 인덱스 검색의 O(log n) 이점은 무의미.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
import traceback
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from indexer import open_db  # noqa: E402
from memory_indexer import embed_text  # noqa: E402

DB_PATH = Path("/Users/yonghaekim/.claude/mindvault-v2/index.db")
DEBUG_LOG = Path("/Users/yonghaekim/.claude/mindvault-v2/debug.log")
RRF_K = 60
DESCRIPTION_WEIGHT = 1.5
DEFAULT_TOP_K = 3
DEFAULT_THRESHOLD = 0.65
EMBED_DIM = 1024
SNIPPET_CHARS = 160


def _debug(msg: str) -> None:
    try:
        DEBUG_LOG.parent.mkdir(parents=True, exist_ok=True)
        with DEBUG_LOG.open("a") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] mem-search: {msg}\n")
    except Exception:
        pass


def rrf_combine(
    vec_results: list[tuple[str, int, str]],
    fts_results: list[tuple[str, int, str]],
    k: int = RRF_K,
) -> dict[str, dict]:
    """RRF로 두 결과 결합.
    vec_results: [(path, rank, kind), ...]  kind ∈ {'body', 'description'}
    fts_results: [(path, rank, ''), ...]
    반환: {path: {"score": float, "source": list[str]}}
    """
    combined: dict[str, dict] = {}

    for path, rank, kind in vec_results:
        weight = DESCRIPTION_WEIGHT if kind == "description" else 1.0
        contribution = weight * (1.0 / (k + rank))
        entry = combined.setdefault(path, {"score": 0.0, "source": []})
        entry["score"] += contribution
        if "vec" not in entry["source"]:
            entry["source"].append("vec")

    for path, rank, _ in fts_results:
        contribution = 1.0 / (k + rank)
        entry = combined.setdefault(path, {"score": 0.0, "source": []})
        entry["score"] += contribution
        if "fts" not in entry["source"]:
            entry["source"].append("fts")

    return combined


def normalize_scores(combined: dict[str, dict]) -> None:
    """min-max 정규화 in-place. 단일 항목이면 1.0. 빈 dict는 no-op."""
    if not combined:
        return
    scores = [v["score"] for v in combined.values()]
    lo, hi = min(scores), max(scores)
    if hi == lo:
        for v in combined.values():
            v["score"] = 1.0
        return
    span = hi - lo
    for v in combined.values():
        v["score"] = (v["score"] - lo) / span


def _fts_escape(query: str) -> str:
    words = re.findall(r"[^\s\"'`*:()]+", query)
    if not words:
        return '""'
    return " ".join(f'"{w}"' for w in words)


def _fts_top_k(
    conn: sqlite3.Connection, query: str, limit: int = 10
) -> list[tuple[str, int, str]]:
    fts_q = _fts_escape(query)
    try:
        rows = conn.execute(
            """
            SELECT path, bm25(memories_fts) AS score
            FROM memories_fts
            WHERE memories_fts MATCH ?
            ORDER BY score LIMIT ?
            """,
            (fts_q, limit),
        ).fetchall()
    except sqlite3.DatabaseError as e:
        _debug(f"fts fail: {e}")
        return []
    return [(r["path"], idx + 1, "") for idx, r in enumerate(rows)]


def _vec_top_k(
    conn: sqlite3.Connection, query_vec: list[float], limit: int = 10
) -> list[tuple[str, int, str]]:
    """BLOB에 저장된 모든 벡터를 numpy로 로드 → cosine top-k.
    반환: [(path, rank, kind), ...] — rank는 1부터.
    """
    rows = list(
        conn.execute("SELECT path, kind, embedding FROM memories_vec")
    )
    if not rows:
        return []
    # 모든 임베딩을 (N, 1024) matrix로
    mat = np.zeros((len(rows), EMBED_DIM), dtype=np.float32)
    meta: list[tuple[str, str]] = []
    for i, r in enumerate(rows):
        arr = np.frombuffer(r["embedding"], dtype=np.float32)
        if arr.shape != (EMBED_DIM,):
            _debug(f"skip bad vec dim {arr.shape} path={r['path']}")
            continue
        mat[i] = arr
        meta.append((r["path"], r["kind"]))
    # 쿼리 정규화 (mean-pooled BGE-M3는 L2-normalized가 아님 → cosine 위해 정규화)
    q = np.asarray(query_vec, dtype=np.float32)
    q_norm = np.linalg.norm(q)
    if q_norm == 0:
        return []
    q = q / q_norm
    # row normalize
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    mat_norm = mat / norms
    sims = mat_norm @ q  # (N,)
    # top-k indices (높은 유사도 = 낮은 distance)
    idx_sorted = np.argsort(-sims)[:limit]
    return [(meta[i][0], rank + 1, meta[i][1]) for rank, i in enumerate(idx_sorted)]


def _snippet(conn: sqlite3.Connection, path: str) -> str:
    row = conn.execute(
        "SELECT body FROM memories_fts WHERE path=?", (path,)
    ).fetchone()
    if not row:
        return ""
    body = row["body"] or ""
    return body[:SNIPPET_CHARS].replace("\n", " ").strip()


def recall_memory(
    query: str,
    top_k: int = DEFAULT_TOP_K,
    score_threshold: float = DEFAULT_THRESHOLD,
    db_path: Path | None = None,
) -> list[dict]:
    """hybrid RRF로 memory/*.md 검색.
    반환: [{"path","name","description","snippet","score","source"}, ...]
    """
    if db_path is None:
        db_path = DB_PATH
    if not db_path.is_file():
        return []

    t0 = time.time()
    try:
        conn = open_db(db_path)
    except Exception as e:
        _debug(f"db open fail: {e}")
        return []

    try:
        fts_rows = _fts_top_k(conn, query, limit=10)

        vec_rows: list[tuple[str, int, str]] = []
        qvec = embed_text(query)
        if qvec is not None:
            vec_rows = _vec_top_k(conn, qvec, limit=10)

        if not vec_rows and not fts_rows:
            _debug(f"no candidates query={query!r}")
            return []

        combined = rrf_combine(vec_rows, fts_rows, k=RRF_K)
        normalize_scores(combined)

        kept = [
            (path, info) for path, info in combined.items()
            if info["score"] >= score_threshold
        ]
        kept.sort(key=lambda x: x[1]["score"], reverse=True)
        kept = kept[:top_k]

        results = []
        for path, info in kept:
            meta = conn.execute(
                "SELECT name, description FROM memories WHERE path=?", (path,)
            ).fetchone()
            results.append({
                "path": path,
                "name": meta["name"] if meta else "",
                "description": meta["description"] if meta else "",
                "snippet": _snippet(conn, path),
                "score": round(info["score"], 4),
                "source": info["source"],
            })

        elapsed = int((time.time() - t0) * 1000)
        rrf_top = [p[:40] for p, _ in kept]
        _debug(
            f"recall query={query!r} vec={len(vec_rows)} fts={len(fts_rows)} "
            f"picked={len(results)} rrf_top={rrf_top} elapsed_ms={elapsed}"
        )
        return results
    except Exception as e:
        _debug(f"recall FATAL: {e}\n{traceback.format_exc()}")
        return []
    finally:
        conn.close()
