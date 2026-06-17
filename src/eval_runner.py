#!/usr/bin/env python3
"""MindVault v3 — eval 러너 (plan §1.3).

qrels 코퍼스를 회수에 돌려 per-query 랭킹 메트릭을 산출한다. 결정성(B5)을 위해
hook/intent 분류기를 우회하고 `recall_memory` 를 직접 호출한다. 순위 품질을 보려고
운영 기본 top_k=1 이 아니라 k(기본 5)로 호출하고, normalized-score 게이트는
끄되(score_threshold=0) raw_cosine 게이트는 운영값 유지(현실 반영).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from memory_search import (  # noqa: E402
    DEFAULT_RAW_COSINE_MIN,
    embed_text,
    recall_memory,
)
from ranking_metrics import score_corpus, score_query  # noqa: E402

QRELS_SCHEMA_VERSION = 1


def load_qrels(path: str | Path) -> dict:
    """qrels JSON 로드 + 스키마 검증(T-B3). 위반 시 ValueError.

    검증: schema_version==1, queries 비어있지 않은 리스트, 각 query 의
    query_id(고유)·query(비어있지 않음)·relevant(비어있지 않은 str 리스트).
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"qrels not found: {p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"qrels invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("qrels root must be an object")
    if data.get("schema_version") != QRELS_SCHEMA_VERSION:
        raise ValueError(
            f"qrels schema_version must be {QRELS_SCHEMA_VERSION}, "
            f"got {data.get('schema_version')!r}"
        )
    queries = data.get("queries")
    if not isinstance(queries, list) or not queries:
        raise ValueError("qrels.queries must be a non-empty list")
    seen_ids: set[str] = set()
    for i, q in enumerate(queries):
        if not isinstance(q, dict):
            raise ValueError(f"queries[{i}] must be an object")
        qid = q.get("query_id")
        if not isinstance(qid, str) or not qid.strip():
            raise ValueError(f"queries[{i}].query_id missing/empty")
        if qid in seen_ids:
            raise ValueError(f"duplicate query_id: {qid}")
        seen_ids.add(qid)
        if not isinstance(q.get("query"), str) or not q["query"].strip():
            raise ValueError(f"{qid}.query missing/empty")
        rel = q.get("relevant")
        if not isinstance(rel, list) or not rel:
            raise ValueError(f"{qid}.relevant must be a non-empty list")
        if not all(isinstance(r, str) and r.strip() for r in rel):
            raise ValueError(f"{qid}.relevant must be non-empty strings")
        et1 = q.get("expected_top1")
        if et1 is not None:
            if not isinstance(et1, str):
                raise ValueError(f"{qid}.expected_top1 must be a string or absent")
            if et1 not in rel:
                raise ValueError(
                    f"{qid}.expected_top1 ({et1!r}) must be a member of relevant {rel}")
    return data


def _arctic_available() -> bool:
    """Arctic-ko(8081) 가동 여부 — 짧은 probe 임베딩으로 감지(B4 graceful)."""
    try:
        return embed_text("probe") is not None
    except Exception:
        return False


def run_corpus(
    qrels_path: str | Path,
    db_path: Path | None = None,
    k: int = 5,
    raw_cosine_min: float = DEFAULT_RAW_COSINE_MIN,
    recall_fn=recall_memory,
    require_arctic: bool = True,
    use_ctx: bool | None = None,
) -> dict:
    """코퍼스 전 쿼리를 회수에 돌려 per-query 메트릭 + 집계 리포트 반환.

    recall_fn 은 테스트 주입용(mock recall). 기본은 실제 recall_memory.
    use_ctx 는 회수 시 CR 임베딩 사용 여부를 *런 시작에 1회 고정*(결정성 B5 — 런 도중
    env 변동에 영향받지 않음). None 이면 MV3_CR_SEARCH env 로 결정하되 그 값을
    리포트에 기록해 baseline 간 비교 가능성을 명시. expand_wikilinks=False: 핵심
    랭커만 측정(wikilink 확장은 직교). Arctic-ko 미가동 → skip 리포트(B4).
    """
    if use_ctx is None:
        use_ctx = os.environ.get("MV3_CR_SEARCH", "0") == "1"
    qrels = load_qrels(qrels_path)
    # mock recall_fn 주입 시(테스트) Arctic probe 생략 — 실서버 없이 결정적.
    if require_arctic and recall_fn is recall_memory and not _arctic_available():
        return {
            "skipped": True,
            "reason": "arctic-ko (8081) unavailable — vec recall 불가",
            "k": k,
            "use_ctx": use_ctx,
            "per_query": [],
            "metrics": score_corpus([], k=k),
        }

    per_query: list[dict] = []
    for q in qrels["queries"]:
        qid = q["query_id"]
        query = q["query"]
        relevant = list(q["relevant"])
        expected_top1 = q.get("expected_top1")
        t0 = time.time()
        results = recall_fn(
            query,
            top_k=k,
            score_threshold=0.0,
            raw_cosine_min=raw_cosine_min,
            db_path=db_path,
            expand_wikilinks=False,
            use_ctx=use_ctx,
        )
        latency_ms = int((time.time() - t0) * 1000)
        retrieved = [r.get("name", "") for r in results]
        metrics = score_query(retrieved, relevant, expected_top1, k)
        per_query.append(
            {
                "query_id": qid,
                "query": query,
                "label": q.get("label"),
                "relevant": relevant,
                "expected_top1": expected_top1,
                "retrieved": retrieved,
                "latency_ms": latency_ms,
                **metrics,
            }
        )
    return {
        "skipped": False,
        "k": k,
        "use_ctx": use_ctx,
        "per_query": per_query,
        "metrics": score_corpus(per_query, k=k),
    }
