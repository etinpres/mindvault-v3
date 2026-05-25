#!/usr/bin/env python3
"""Sprint 6 검증 — "도메인 쿼리의 top3에 무관 세션이 끼는지" 측정.

각 RELEVANT 쿼리 → search.recall() → top3 세션의 head 출력.
사람이 보고 도메인 일치 여부 채점 → precision@3.

기존 검증(NOISE vs RELEVANT raw cosine 비교)과 별개. 백로그 168줄
원래 의도("도메인 쿼리 top1~3에 무관 세션이 끼는 현상") 정조준.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from indexer import extract_head_turns_body  # noqa: E402
from search import recall, _embed_query, vec_candidates, fts_candidates  # noqa: E402
import sqlite3  # noqa: E402

# v3.2.7: production state pollution 방지. MV3_DATA_DIR env var 우선.
DB_PATH = Path(os.environ.get("MV3_DATA_DIR", "~/.claude/mindvault-v3")).expanduser() / "index.db"

# RELEVANT 쿼리 — 사용자가 실제로 작업한 도메인. 한 줄 쿼리는 사용자가 회수하려는
# 의도를 모방. 도메인이 너무 좁으면 매칭 어려워 의도가 잘 드러나는 한 줄.
RELEVANT_QUERIES = [
    "메일 보내는 sendmail SMTP 설정",
    "EPSON 스캐너 CLI 자동 크롭",
    "유튜브 영상 카드뉴스 제작",
    "MindVault 임베딩 게이트 sprint",
    "Karpathy LLM Wiki RAG 정리",
    "Higgsfield product photoshoot 프롬프트",
    "Assemble landing 페이지 다크 테마",
    "grammar saas 중학 영문법 망각곡선",
    "Hyperframes 비디오 렌더 fonts",
    "텔레그램 bot tg_notify 자동 응답",
]


def fetch_head(session_id: str) -> str:
    """sessions 테이블에서 file_path 가져와 head-4-turn 추출. stale이면 빈문자."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        row = conn.execute(
            "SELECT file_path FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if not row:
            return ""
        fp = Path(row[0])
        if not fp.is_file():
            return "(stale: file deleted)"
        return extract_head_turns_body(fp)
    finally:
        conn.close()


def measure_query(query: str, top_k: int = 3) -> dict:
    """recall() 호출하고 top-k 세션의 head + raw cosine 같이 반환."""
    # raw cosine map 얻기 위해 search 내부 함수 직접 호출
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    qvec = _embed_query(query)
    fts_list = fts_candidates(conn, query, limit=10)
    vec_list, raw_map = ([], {})
    if qvec is not None:
        vec_list, raw_map = vec_candidates(conn, qvec, limit=10)
    conn.close()

    results = recall(query, top_k=top_k)
    out_items = []
    for r in results:
        sid = r["session_id"]
        head = fetch_head(sid)
        out_items.append({
            "session_id": sid[:8],
            "first_ts": r.get("first_ts", "")[:16],
            "raw_cosine": raw_map.get(sid, 0.0),
            "summary": r.get("summary", "") or "",
            "head_preview": head[:500],
        })
    # 쿼리 자체의 top1 raw cosine
    top1_raw = max(raw_map.values()) if raw_map else 0.0
    return {
        "query": query,
        "top1_raw": top1_raw,
        "fts_hits": len(fts_list),
        "vec_hits": len(vec_list),
        "results": out_items,
    }


def main() -> None:
    print("=" * 75)
    print("Sprint 6 검증: 도메인 쿼리 → top3 세션 (head-4-turn 임베딩)")
    print("=" * 75)
    for q in RELEVANT_QUERIES:
        t0 = time.time()
        m = measure_query(q)
        elapsed = time.time() - t0
        print(f"\n### {q}")
        print(f"top1_raw={m['top1_raw']:.3f}  fts={m['fts_hits']}  vec={m['vec_hits']}  elapsed={elapsed:.1f}s")
        if not m["results"]:
            print("  (no results — gate blocked)")
            continue
        for i, r in enumerate(m["results"], 1):
            print(f"\n  [{i}] sid={r['session_id']} ts={r['first_ts']} raw={r['raw_cosine']:.3f}")
            head = r["head_preview"].replace("\n", " ⏎ ")[:400]
            print(f"      head: {head}")
            if r["summary"]:
                summ = r["summary"].replace("\n", " ⏎ ")[:200]
                print(f"      summary: {summ}")


if __name__ == "__main__":
    main()
