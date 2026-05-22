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
from memory_indexer import embed_text as _embed_text_base  # noqa: E402


def embed_text(query: str) -> list[float] | None:
    """쿼리 임베딩 wrapper — Arctic-ko 학습 설정상 "query: " prefix 자동 부착."""
    return _embed_text_base(query, kind="query")

DB_PATH = Path("/Users/yonghaekim/.claude/mindvault-v2/index.db")
DEBUG_LOG = Path("/Users/yonghaekim/.claude/mindvault-v2/debug.log")
RRF_K = 60
DESCRIPTION_WEIGHT = 1.5
DEFAULT_TOP_K = 1  # 보수적: 절대 우수한 1건만. V1 토큰 낭비 회피.
DEFAULT_THRESHOLD = 0.65  # normalize 후 점수 게이트 (보조)
DEFAULT_RAW_COSINE_MIN = 0.40  # Sprint 9 Arctic-ko 분포에 맞춰 재튜닝 (도메인 0.44~0.61 vs 잡담 0.23~0.34, gap 0.26)
EMBED_DIM = 1024
SNIPPET_CHARS = 160
# Sprint 7: top-k hit의 [[slug]] wikilink를 1-hop 확장. 메모리 그래프 신호 활용.
WIKILINK_RE = re.compile(r"\[\[([a-z0-9_-]+)\]\]")
WIKILINK_EXPAND_MAX = 2  # 1건 hit + 1-hop 2건 = 최대 3건. V1 토큰 낭비 방지선.


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
) -> tuple[list[tuple[str, int, str]], dict[str, float]]:
    """BLOB에 저장된 모든 벡터를 numpy로 로드 → cosine top-k.
    반환: ([(path, rank, kind), ...], {path: max_raw_cosine})
    raw_cosine_map은 V1-style 토큰 낭비 차단을 위한 absolute relevance 게이트용.
    """
    rows = list(
        conn.execute("SELECT path, kind, embedding FROM memories_vec")
    )
    if not rows:
        return [], {}
    mat = np.zeros((len(rows), EMBED_DIM), dtype=np.float32)
    meta: list[tuple[str, str]] = []
    for i, r in enumerate(rows):
        arr = np.frombuffer(r["embedding"], dtype=np.float32)
        if arr.shape != (EMBED_DIM,):
            _debug(f"skip bad vec dim {arr.shape} path={r['path']}")
            continue
        mat[i] = arr
        meta.append((r["path"], r["kind"]))
    q = np.asarray(query_vec, dtype=np.float32)
    q_norm = np.linalg.norm(q)
    if q_norm == 0:
        return [], {}
    q = q / q_norm
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    mat_norm = mat / norms
    sims = mat_norm @ q  # (N,) raw cosine [0..1]
    idx_sorted = np.argsort(-sims)[:limit]
    results = [(meta[i][0], rank + 1, meta[i][1]) for rank, i in enumerate(idx_sorted)]
    # path별 최대 raw cosine (body/description 둘 중 큰 값) — 게이트용
    raw_map: dict[str, float] = {}
    for i in idx_sorted:
        path = meta[i][0]
        sim = float(sims[i])
        if sim > raw_map.get(path, -1.0):
            raw_map[path] = sim
    return results, raw_map


def _snippet(conn: sqlite3.Connection, path: str) -> str:
    row = conn.execute(
        "SELECT body FROM memories_fts WHERE path=?", (path,)
    ).fetchone()
    if not row:
        return ""
    body = row["body"] or ""
    return body[:SNIPPET_CHARS].replace("\n", " ").strip()


def _resolve_wikilink(conn: sqlite3.Connection, slug: str) -> dict | None:
    """[[slug]] → memories 테이블 row. 매칭 우선:
    1. memories.name == slug (frontmatter `name:` 슬러그 직접 매칭)
    2. path basename(.md 제외, '-' → '_') == slug.replace('-','_')
    실패 시 None.
    """
    row = conn.execute(
        "SELECT path, name, description FROM memories WHERE name=?", (slug,)
    ).fetchone()
    if row:
        return dict(row)
    snake = slug.replace("-", "_")
    candidates = conn.execute(
        "SELECT path, name, description FROM memories WHERE path LIKE ?",
        (f"%/{snake}.md",),
    ).fetchall()
    if candidates:
        return dict(candidates[0])
    return None


def _expand_wikilinks(
    conn: sqlite3.Connection,
    results: list[dict],
    max_expansion: int = WIKILINK_EXPAND_MAX,
) -> list[dict]:
    """results 각각의 body에서 [[slug]] 추출 → 1-hop 확장.

    이미 results에 포함된 path는 skip. 동일 slug 중복 skip. 최대 max_expansion 건.
    expanded item shape: results와 동일 + source=['wikilink-1hop'] + via='<원본 name>'.
    """
    if max_expansion <= 0 or not results:
        return []
    seen_paths = {r["path"] for r in results}
    seen_slugs: set[str] = set()
    expanded: list[dict] = []
    for r in results:
        if len(expanded) >= max_expansion:
            break
        body_row = conn.execute(
            "SELECT body FROM memories_fts WHERE path=?", (r["path"],)
        ).fetchone()
        body = body_row["body"] if body_row else (r.get("snippet") or "")
        for m in WIKILINK_RE.finditer(body):
            if len(expanded) >= max_expansion:
                break
            slug = m.group(1)
            if slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            resolved = _resolve_wikilink(conn, slug)
            if not resolved or resolved["path"] in seen_paths:
                continue
            seen_paths.add(resolved["path"])
            expanded.append({
                "path": resolved["path"],
                "name": resolved["name"] or slug,
                "description": resolved["description"] or "",
                "snippet": _snippet(conn, resolved["path"]),
                "score": 0.0,
                "raw_cosine": 0.0,
                "source": ["wikilink-1hop"],
                "via": r["name"],
            })
    return expanded


def recall_memory(
    query: str,
    top_k: int = DEFAULT_TOP_K,
    score_threshold: float = DEFAULT_THRESHOLD,
    raw_cosine_min: float = DEFAULT_RAW_COSINE_MIN,
    db_path: Path | None = None,
    expand_wikilinks: bool = True,
) -> list[dict]:
    """hybrid RRF + raw vec cosine 게이트 memory 검색.

    raw_cosine_min: vec top-1의 raw cosine이 이 값 미만이면 결과 0건.
                    V1 토큰 낭비 회피 (BGE-M3는 잡담에도 0.6-0.75 매칭 → 0.78+ 만 통과).
    반환: [{"path","name","description","snippet","score","raw_cosine","source"}, ...]
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
        raw_cosine_map: dict[str, float] = {}
        qvec = embed_text(query)
        if qvec is not None:
            vec_rows, raw_cosine_map = _vec_top_k(conn, qvec, limit=10)

        if not vec_rows and not fts_rows:
            _debug(f"no candidates query={query!r}")
            return []

        top1_raw = max(raw_cosine_map.values()) if raw_cosine_map else 0.0
        combined = rrf_combine(vec_rows, fts_rows, k=RRF_K)
        normalize_scores(combined)

        # raw cosine 게이트: vec-only hit + raw < min은 차단. fts hit은 면제 (BM25 보장).
        # normalize score는 ranking용 보조 — 절대 차단 X (raw 통과한 path끼리만 비교).
        kept = []
        for path, info in combined.items():
            raw = raw_cosine_map.get(path, 0.0)
            if raw_cosine_min > 0 and raw < raw_cosine_min and "fts" not in info["source"]:
                continue
            # normalize score는 ranking signal로만 사용
            kept.append((path, info, raw))
        # 정렬: raw cosine 우선 (절대 관련도) → 동률 시 normalize score
        kept.sort(key=lambda x: (x[2], x[1]["score"]), reverse=True)
        kept = kept[:top_k]

        results = []
        for path, info, raw in kept:
            meta = conn.execute(
                "SELECT name, description FROM memories WHERE path=?", (path,)
            ).fetchone()
            results.append({
                "path": path,
                "name": meta["name"] if meta else "",
                "description": meta["description"] if meta else "",
                "snippet": _snippet(conn, path),
                "score": round(info["score"], 4),
                "raw_cosine": round(raw, 4),
                "source": info["source"],
            })

        # Sprint 7: wikilink 1-hop 확장 (게이트 통과한 results 기준).
        # 토큰 절약 위해 results가 있을 때만 호출. body에서 [[slug]] 매칭.
        if expand_wikilinks and results:
            expanded = _expand_wikilinks(conn, results)
            if expanded:
                results = results + expanded

        elapsed = int((time.time() - t0) * 1000)
        rrf_top = [p[:40] for p, _, _ in kept]
        _debug(
            f"recall query={query!r} vec={len(vec_rows)} fts={len(fts_rows)} "
            f"top1_raw={top1_raw:.3f} picked={len(results)} "
            f"rrf_top={rrf_top} elapsed_ms={elapsed}"
        )
        return results
    except Exception as e:
        _debug(f"recall FATAL: {e}\n{traceback.format_exc()}")
        return []
    finally:
        conn.close()
