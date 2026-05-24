#!/usr/bin/env python3
"""MindVault v3 Sprint 4 — hybrid RRF memory 검색.

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

DB_PATH = Path("~/.claude/mindvault-v3/index.db").expanduser()
DEBUG_LOG = Path("~/.claude/mindvault-v3/debug.log").expanduser()
RRF_K = 60
DESCRIPTION_WEIGHT = 1.5
DEFAULT_TOP_K = 1  # 보수적: 절대 우수한 1건만. V1 토큰 낭비 회피.
DEFAULT_THRESHOLD = 0.50  # NEXT-29 (2026-05-24): 0.65 → 0.50. 주의: 현재 recall_memory 함수 body 에서 미적용 (dead param). 실 적용은 NEXT-30 에서. 사유는 hooks/memory-recall.py SCORE_THRESHOLD 주석 참조.
DEFAULT_RAW_COSINE_MIN = 0.32  # NEXT-30.1 (2026-05-24): 0.40 → 0.32. 측정 근거는 hooks/memory-recall.py RAW_COSINE_MIN_DEFAULT 주석 참조.
# Sprint NEXT-4 — procedural type 별 게이트 보너스. 명령어 syntax 메모리는
# specific keyword 매칭 강도가 일반 결정·프로젝트 메모리보다 엄격해야 정확함.
# default(0.40) → procedural 0.45, hinted(0.32) → procedural 0.37 로 자동 분리.
PROCEDURAL_GATE_BONUS = 0.05
PROCEDURAL_PATH_MARKER = "/_procedural/"
EMBED_DIM = 1024
SNIPPET_CHARS = 600
# Sprint 11: 160→600. 회수 1건만 출력하므로 토큰 부담은 ~150 tokens 증가.
# 이전 160자는 frontmatter 마무리 + 첫 한 줄만 잡혀 핵심 본문(수치·결정 사항)
# 미포함 → Claude가 추가 Read 호출. net token 절약을 위해 발췌를 한 호흡에.
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


# NEXT-31 (2026-05-24): alias_index 캐시 + lookup helper.
# alias_generator.py 가 SessionEnd/수동 trigger 로 Gemma batch 호출하여
# ~/.claude/mindvault-v3/alias_index.json 에 메모리당 별칭 5개 사전 캐시.
# 검색 시 latency 0 lookup — JSON 파일 read + 토큰 set 교집합.
_ALIAS_INDEX_CACHE: dict | None = None
_ALIAS_INDEX_MTIME: float = 0.0
ALIAS_INDEX_PATH = Path("~/.claude/mindvault-v3/alias_index.json").expanduser()
ALIAS_BOOST_TOKEN_MIN = 2  # 1자 토큰은 false positive 위험 (한국어 "안","함")


def _load_alias_index() -> dict:
    """mtime 기반 lazy reload. 캐시 hit 시 즉시 반환."""
    global _ALIAS_INDEX_CACHE, _ALIAS_INDEX_MTIME
    try:
        mt = ALIAS_INDEX_PATH.stat().st_mtime
    except OSError:
        return {}
    if _ALIAS_INDEX_CACHE is not None and mt == _ALIAS_INDEX_MTIME:
        return _ALIAS_INDEX_CACHE
    try:
        _ALIAS_INDEX_CACHE = json.loads(ALIAS_INDEX_PATH.read_text())
        _ALIAS_INDEX_MTIME = mt
    except (json.JSONDecodeError, OSError):
        _ALIAS_INDEX_CACHE = {}
    return _ALIAS_INDEX_CACHE


def _alias_boost_paths(query: str) -> set[str]:
    """query 토큰들 중 어떤 메모리의 alias 와 정확 일치하는 path 셋.

    매칭 규칙 (엄격):
    - alias 토큰 (2자+) 과 query 토큰 (2자+) 의 정확 set 교집합만 인정
    - substring 매칭 X — 회귀(잘못된 boost) 차단
    - 한 메모리당 최대 1번만 추가
    """
    idx = _load_alias_index()
    if not idx:
        return set()
    q_tokens = {
        t.lower() for t in re.findall(r"[가-힣A-Za-z0-9]+", query)
        if len(t) >= ALIAS_BOOST_TOKEN_MIN
    }
    if not q_tokens:
        return set()
    matched: set[str] = set()
    for path, info in idx.items():
        for alias in info.get("aliases", []):
            a_lower = alias.lower()
            a_tokens = {
                t for t in re.findall(r"[가-힣A-Za-z0-9]+", a_lower)
                if len(t) >= ALIAS_BOOST_TOKEN_MIN
            }
            # 토큰 set 정확 교집합 — substring 회귀 ("영상 리포트" alias 가
            # "리포트 정리" query 를 잡아 ranking 거꾸로 만드는 케이스 차단)
            if a_tokens and a_tokens & q_tokens:
                matched.add(path)
                break
    return matched


_FTS_TOKEN_RE = re.compile(r"[가-힣A-Za-z0-9_]+")


def _fts_escape(query: str) -> str:
    """FTS5 MATCH 쿼리 생성.

    NEXT-30.2 (2026-05-24): 이전 동작 `"word1" "word2"` 는 FTS5 의 implicit
    AND 라 모든 토큰이 정확히 일치해야 hit. 한국어 활용형/조사/공백 변형으로
    "스캔해" vs "스캐너" 같은 변형이 no_candidates 870건의 큰 슬라이스였음.
    새 동작:
    - 한글/영문/숫자만 토큰으로 인정 (`.~?/-:` 같은 특수문자는 FTS5 syntax 충돌)
    - 단독 숫자 토큰 제외 (`33` 같은 숫자는 FTS5에서 column 참조로 해석)
    - 2자 이상 토큰만 사용 (1자 토큰 — 한국어 "안","함" 등 — 은 noise)
    - 각 토큰에 prefix wildcard (`word*`) 적용
    - OR 결합 — 하나라도 잡히면 candidate. RRF + raw_cosine 게이트가 false
      positive 차단.

    post-ship fix (2026-05-24): 이전 `[^\\s\"'\\`*:()]+` 는 `.~?/-` 와 숫자를
    그대로 흘려보내 'syntax error near "?"', 'no such column: 33' 등
    debug.log 67건 fail 누적. 알파넘 화이트리스트로 전환.
    """
    words = _FTS_TOKEN_RE.findall(query)
    pat = [f"{w}*" for w in words if len(w) >= 2 and not w.isdigit()]
    if not pat:
        # 모든 토큰이 무효(공백·특수문자·1자·순수숫자) → 빈 매치
        return '""'
    return " OR ".join(pat)


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
        emb = r["embedding"]
        if not emb:
            # NEXT-28 sentinel — 빈 bytes 는 의도된 무한-재시도 차단. silent.
            continue
        arr = np.frombuffer(emb, dtype=np.float32)
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
    # Sprint 11: raw_map은 limit과 무관하게 전체 path × kind 의 최대 cosine 보유.
    # wikilink 1-hop expansion 게이트(B)가 top-K 밖 target도 cosine 확인 가능하도록.
    # 게이트 통과 못한 path는 expand_wikilinks에서 차단 → 무관 메모리 노이즈 0.
    # 메인 raw_cosine 게이트(recall_memory)는 path 단위로 max 보고 동작 — 동일.
    raw_map: dict[str, float] = {}
    for i, (path, _kind) in enumerate(meta):
        sim = float(sims[i])
        if sim > raw_map.get(path, -1.0):
            raw_map[path] = sim
    return results, raw_map


SNIPPET_WORD_RE = re.compile(r"[가-힣A-Za-z0-9]{2,}")


BROAD_WORD_FREQ_LIMIT = 5
# Sprint 11: 본문에 5회 초과 등장하는 query word는 broad/generic으로 보고 제외.
# 메모리 이름과 동일한 단어(예: "mindvault")가 본문 헤더·전반에 박혀있으면
# specific keyword(예: "모델") 한 번 매치가 broad 매치 cluster에 밀려 무효화됨.
# broad word 제외 후 specific 매치 위치 기반 window 잡으면 query 의도 발췌 가능.


def _query_window(body: str, query: str, char_budget: int) -> str | None:
    """query 단어들의 본문 등장 위치 중 가장 밀집된 지점 ±char_budget//2 window.
    한글/영문/숫자 길이>=2 토큰만 인정 (조사·단음절 노이즈 차단).
    본문에 BROAD_WORD_FREQ_LIMIT 초과 등장하는 broad/generic word는 제외.
    찾지 못하면 None → caller가 body[:char_budget] fallback.
    Sprint 11: 본문 시작만 자르면 최신 sprint 정보를 못 잡는 한계 회피.
    """
    words = SNIPPET_WORD_RE.findall(query)
    if not words:
        return None
    body_lower = body.lower()
    positions_by_word: dict[str, list[int]] = {}
    for w in words:
        lw = w.lower()
        plist: list[int] = []
        start = 0
        while True:
            pos = body_lower.find(lw, start)
            if pos < 0:
                break
            plist.append(pos)
            start = pos + len(lw)
        if plist:
            positions_by_word[w] = plist
    if not positions_by_word:
        return None
    # broad word 제외. 다 broad면 가장 freq 낮은 단어만 채택 (graceful degrade).
    specific = {w: p for w, p in positions_by_word.items() if len(p) <= BROAD_WORD_FREQ_LIMIT}
    if not specific:
        min_w = min(positions_by_word, key=lambda w: len(positions_by_word[w]))
        specific = {min_w: positions_by_word[min_w]}
    positions: list[int] = sorted(p for plist in specific.values() for p in plist)
    half = char_budget // 2
    # 각 매치 위치를 window 중심 후보로 두고 그 window 안 매치 개수 카운트.
    # 동률이면 더 늦은(최신 sprint일 가능성 높은) 위치 선호.
    best_pos = positions[0]
    best_count = -1
    for p in positions:
        lo, hi = p - half, p + half
        count = sum(1 for q in positions if lo <= q <= hi)
        if count > best_count or (count == best_count and p > best_pos):
            best_count = count
            best_pos = p
    start = max(0, best_pos - half)
    end = start + char_budget
    snippet = body[start:end].replace("\n", " ").strip()
    if start > 0:
        snippet = "…" + snippet
    return snippet


def _snippet(conn: sqlite3.Connection, path: str, query: str | None = None) -> str:
    row = conn.execute(
        "SELECT body FROM memories_fts WHERE path=?", (path,)
    ).fetchone()
    if not row:
        return ""
    body = row["body"] or ""
    if query:
        window = _query_window(body, query, SNIPPET_CHARS)
        if window is not None:
            return window
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


def _is_procedural_path(path: str) -> bool:
    """memory path 가 _procedural/ slot 안인지. Sprint NEXT-4 type 게이트 분기."""
    return PROCEDURAL_PATH_MARKER in (path or "")


def _gate_for_path(path: str, base_min: float) -> float:
    """type 별 raw_cosine 게이트. procedural 은 +PROCEDURAL_GATE_BONUS 엄격."""
    if base_min <= 0:
        return base_min
    if _is_procedural_path(path):
        return base_min + PROCEDURAL_GATE_BONUS
    return base_min


WIKILINK_GATE_FACTOR = 0.75
# Sprint 11: wikilink target의 raw_cosine이 raw_cosine_min × 0.75 미만이면 expand 차단.
# 예: raw_cosine_min=0.40 → wikilink 게이트=0.30. query와 가장 약하게라도 관련된
# target만 1-hop 허용. 이전엔 target 관련도 미측정 → 무관 메모리 1-hop으로 끌려옴
# (예: project-mindvault 히트 → feedback-transcript-lone-surrogate noise).


def _expand_wikilinks(
    conn: sqlite3.Connection,
    results: list[dict],
    raw_cosine_map: dict[str, float],
    raw_cosine_min: float,
    query: str | None = None,
    max_expansion: int = WIKILINK_EXPAND_MAX,
) -> list[dict]:
    """results 각각의 body에서 [[slug]] 추출 → 1-hop 확장.

    Sprint 11 변경:
    - target path의 raw_cosine이 raw_cosine_min × WIKILINK_GATE_FACTOR 미만이면 skip.
      raw_cosine_map은 전체 path 커버하므로 top-K 밖 target도 정확히 게이트.
    - snippet 생성에 query 전달 (sliding window 적용).

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
            target_raw = raw_cosine_map.get(resolved["path"], 0.0)
            # Sprint NEXT-4: target type 별 게이트 분기. procedural target 은
            # base 보너스 + WIKILINK_GATE_FACTOR 둘 다 적용 → 더 엄격.
            path_base = _gate_for_path(resolved["path"], raw_cosine_min)
            gate = path_base * WIKILINK_GATE_FACTOR if path_base > 0 else 0.0
            if gate > 0 and target_raw < gate:
                _debug(
                    f"wikilink gate block slug={slug} target_raw={target_raw:.3f} "
                    f"gate={gate:.3f}"
                )
                continue
            seen_paths.add(resolved["path"])
            expanded.append({
                "path": resolved["path"],
                "name": resolved["name"] or slug,
                "description": resolved["description"] or "",
                "snippet": _snippet(conn, resolved["path"], query=query),
                "score": 0.0,
                "raw_cosine": round(target_raw, 4),
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

        # NEXT-32 (2026-05-24): alias_index lookup 정식 통합. NEXT-31 에서
        # Gemma 생성 alias 로 시도했다 cohort 회귀 (WEAK 5/15→3/15) 일으켰던
        # 작업을 Sonnet 4.6 으로 alias 재생성 후 재시도. 새 alias 품질:
        #   (a) description 단어 재사용 차단
        #   (b) "브이3"/"v3"/"기획안 웹으로" 같은 진짜 우회 표현 등장
        # 통합 방식 (회귀 방지):
        #   - alias 매칭 path 를 candidates 에 fallback 으로만 추가
        #   - raw_cosine sentinel 0.35 (게이트 통과)
        #   - score 0.0 으로 시작 — RRF/normalize 후 vec/fts hit 의 더 강한
        #     score 가 자연스럽게 위. 즉 alias 는 "임베딩 누락 회복" 역할만.
        # 테스트 격리: prod DB_PATH 일 때만 lookup. tmp_db (테스트 fixture)
        # 에서는 alias_index 와 path 가 안 맞아 false hit 회귀 가능.
        if db_path == DB_PATH:
            alias_paths = _alias_boost_paths(query)
        else:
            alias_paths = set()

        if not vec_rows and not fts_rows and not alias_paths:
            _debug(f"no candidates query={query!r}")
            return []

        top1_raw = max(raw_cosine_map.values()) if raw_cosine_map else 0.0
        combined = rrf_combine(vec_rows, fts_rows, k=RRF_K)

        # alias fallback 합류 — 신규 path 추가 + 이미 후보인 path 의 raw 도
        # 게이트 통과 보장. setdefault 만으로는 vec raw 0.30 정도의 약 매칭이
        # 이미 들어가 있을 때 sentinel 0.35 가 무시되어 게이트 떨굼 (NEXT-32
        # 측정에서 "기획안 만들어줘" → html-output-default raw 0.298 떨굼 발견).
        # max(현재, 0.35) 로 upgrade — alias 매칭 = "의미 매칭 강함" 신호.
        # score 는 손대지 않아 RRF 정상 ranking 유지.
        for bp in alias_paths:
            cur = raw_cosine_map.get(bp, 0.0)
            raw_cosine_map[bp] = max(cur, 0.35)
            if bp not in combined:
                combined[bp] = {"score": 0.0, "source": ["alias"]}
            else:
                if "alias" not in combined[bp]["source"]:
                    combined[bp]["source"].append("alias")

        normalize_scores(combined)

        # raw cosine 게이트: 의미적 무관 path 차단.
        # Sprint 12: fts source도 raw_cosine 검사 적용 (단어 우연 매칭으로 잡담이
        # fts-only hit으로 통과하는 회귀 차단). fts source인 경우 임계를 절반으로
        # 완화 — 정확 keyword 매칭은 raw 약해도 정당하지만 raw 0.20 미만은 잡담 영역.
        # NEXT-30.2 (2026-05-24): _fts_escape 가 prefix OR 로 candidates 영역을
        # 크게 넓혀 fts-only 비율 0.5 배가 너무 헐거워졌음 ("음 그래서"→
        # user-english-teacher raw 0.223, "이거 뭐였지"→scan-natural-language
        # raw 0.197 통과 회귀). 0.5 → 0.8 로 좁힘.
        # vec 임베딩이 아예 실패(서버 다운)했으면 게이트 면제 — fts-only fallback 허용.
        vec_available = bool(raw_cosine_map)
        kept = []
        for path, info in combined.items():
            raw = raw_cosine_map.get(path, 0.0)
            if raw_cosine_min > 0 and vec_available:
                # Sprint NEXT-4: type 별 게이트 — procedural 은 +0.05 엄격.
                # specific keyword 매칭 강도가 일반 결정 메모리보다 필요한 영역.
                path_gate = _gate_for_path(path, raw_cosine_min)
                has_vec = "vec" in info["source"]
                threshold = path_gate if has_vec else path_gate * 0.8
                if raw < threshold:
                    continue
            # NEXT-30.3 (2026-05-24): score_threshold 게이트 정식 적용. 이전엔
            # 함수 파라미터만 받고 미적용 (dead param) 이었음. top_k 자르기 전
            # combined 단에서 적용 — top-1 만 보더라도 wikilink 1-hop expansion
            # 의 후속 결과를 차단하는 효과. top-1 자체는 normalize 후 score=1.0
            # 라 게이트 통과.
            if score_threshold > 0 and info["score"] < score_threshold:
                continue
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
                "snippet": _snippet(conn, path, query=query),
                "score": round(info["score"], 4),
                "raw_cosine": round(raw, 4),
                "source": info["source"],
            })

        # Sprint 7: wikilink 1-hop 확장 (게이트 통과한 results 기준).
        # Sprint 11: target의 raw_cosine 게이트 추가 — 무관 메모리 차단.
        if expand_wikilinks and results:
            expanded = _expand_wikilinks(
                conn,
                results,
                raw_cosine_map=raw_cosine_map,
                raw_cosine_min=raw_cosine_min,
                query=query,
            )
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
        _debug(f"recall FATAL: {type(e).__name__}: {e}\n{traceback.format_exc()}")
        return []
    finally:
        conn.close()
