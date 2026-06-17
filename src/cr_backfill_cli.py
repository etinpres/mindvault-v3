#!/usr/bin/env python3
"""MindVault v3 — Contextual Retrieval 백필 CLI (plan §6).

이미 인덱싱된 메모리(raw embedding 존재)에 contextual 임베딩(embedding_ctx)을 채운다.
`corpus_generation` 이 현재 설정 모드의 해시와 다른(또는 NULL) 메모리만 재처리 →
멱등·재개 가능(중단 후 재실행 안전). `memory_indexer.full_rebuild` 는 raw 만 채우고,
이 CLI 는 ctx 컬럼을 채운다(역할 분리).

사용:
    MV3_DATA_DIR=~/.claude/mindvault-v3 python3 -m src.cr_backfill_cli \
        --cr-backfill --mode synopsis [--limit N] [--dry-run] [--sleep 0.0]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from indexer import open_db  # noqa: E402
from memory_indexer import (  # noqa: E402
    CR_MODES,
    _acquire_lock,
    _debug,
    _parse_memory_file,
    _release_lock,
    compute_contextual_embedding,
    compute_corpus_generation,
)

BACKFILL_MODES = ("title", "synopsis")  # off 는 백필 대상 아님(ctx 미사용)


def cr_backfill(
    mode: str,
    db_path: Path | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    sleep_s: float = 0.0,
) -> dict:
    """corpus_generation stale(또는 NULL) 메모리만 ctx 재임베딩.

    반환: {"candidates", "processed", "skipped_unreadable", "dry_run"}.
    설정 모드(mode) 기준 generation 으로 마킹 → 강등돼도 재실행 시 수렴(skip).
    """
    if mode not in BACKFILL_MODES:
        raise ValueError(f"mode must be one of {BACKFILL_MODES}, got {mode!r}")
    target_gen = compute_corpus_generation(mode)
    counts = {"candidates": 0, "processed": 0, "skipped_unreadable": 0,
              "skipped_stale": 0, "failed_embed": 0, "dry_run": dry_run, "lock_busy": False}
    # adversarial review 2026-06-17 (R8): incremental_index 와 동일 memory-indexer.lock
    # 으로 직렬화. 무락 백필 ↔ 동시 hook reindex 의 lost-update(백필이 body v1 ctx 를
    # v2 raw 행에 덮어써 ctx/raw 영구 불일치 + gen(mode) 가짜 converged → 양 경로 영영
    # 미수정)을 차단. 락 busy 면 abort(다음 기회 재시도) — incremental_index 와 동형.
    lock = _acquire_lock(db_path)
    if lock is None:
        _debug("cr_backfill: indexer lock busy — abort")
        counts["lock_busy"] = True
        return counts
    try:
        conn = open_db(db_path) if db_path is not None else open_db()
        try:
            rows = conn.execute(
                "SELECT path, name, description, mtime_ns FROM memories "
                "WHERE corpus_generation IS NULL OR corpus_generation != ? "
                "ORDER BY path",
                (target_gen,),
            ).fetchall()
            if limit is not None:
                rows = rows[:limit]
            counts["candidates"] = len(rows)

            for r in rows:
                path = r["path"]
                # raw 인덱스가 stale(파일 편집 후 incremental_index 미실행)이면 백필이
                # 현재 파일로 ctx 를 만들어 ctx(v2)/raw(v1) 혼합 + converged 마킹을 한다
                # (codex 2-track R11). mtime 불일치 메모리는 skip — incremental_index 가
                # 먼저 raw 를 갱신하게 두고, 그 뒤 백필이 처리(자가치유).
                try:
                    cur_mtime = Path(path).stat().st_mtime_ns
                except OSError:
                    counts["skipped_unreadable"] += 1
                    continue
                if cur_mtime != r["mtime_ns"]:
                    counts["skipped_stale"] += 1
                    continue
                parsed = _parse_memory_file(Path(path))
                if parsed is None:
                    counts["skipped_unreadable"] += 1
                    continue
                fm, body = parsed
                name = str(fm.get("name") or Path(path).stem)
                description = str(fm.get("description") or "")

                if dry_run:
                    counts["processed"] += 1
                    continue

                embedding_ctx, cr_synopsis, effective_mode = compute_contextual_embedding(
                    name, description, body, mode
                )
                # adversarial review 2026-06-17 (R2): 임베딩 서버 다운으로 ctx 생성 실패하면
                # compute_contextual_embedding 이 (None,None,"off") 를 반환한다. body 가
                # 있는데 effective="off" = ctx 임베딩 실패 → 영구 converged 마커(corpus_
                # generation=target_gen)를 쓰면 다음 백필이 영영 제외(indexer.py
                # EmbedUnavailable 가드와 동일 클래스). 마커 미기록 + 다음 run 재시도하도록 skip.
                if effective_mode == "off" and body.strip():
                    counts["failed_embed"] += 1
                    continue
                # body vec 행의 ctx 컬럼만 갱신(raw embedding 불변). body 없으면 0행 영향.
                conn.execute(
                    "UPDATE memories_vec SET embedding_ctx=?, cr_synopsis=? "
                    "WHERE path=? AND kind='body'",
                    (embedding_ctx, cr_synopsis, path),
                )
                # corpus_generation 은 *실제 달성 tier*(effective_mode) 기준 — 설정모드
                # 아님. adversarial review 2026-06-17 (R12): 설정 synopsis 인데 Gemma
                # 일시중단으로 title 강등(effective="title")되면 target_gen(=gen(synopsis))로
                # 마킹 시 Gemma 복구 후에도 후보 SELECT(!=gen(synopsis))에서 제외돼 영구
                # title 고정. effective 기준이면 gen("title")≠gen("synopsis") 라 다음 백필
                # 후보로 남아 재시도된다. SELECT 필터는 target_gen(설정모드) 유지.
                # 단 *빈 body*(effective="off" & body 없음 — ctx 구조적 불가)는 설정모드
                # 기준(target_gen)으로 수렴 마킹 — R13: gen(effective)=gen("off")≠target_gen
                # 이라 빈-body 가 매 run 재선정되는 무한 no-op(수렴 깨짐). 비-빈 body 의
                # effective="off"(임베딩 실패)는 위 line-109 가드가 이미 continue 로 차단.
                _gen = target_gen if (effective_mode == "off" and not body.strip()) \
                    else compute_corpus_generation(effective_mode)
                conn.execute(
                    "UPDATE memories SET cr_mode=?, corpus_generation=? WHERE path=?",
                    (effective_mode, _gen, path),
                )
                conn.commit()
                counts["processed"] += 1
                # Gemma rate 보호 — synopsis 모드는 메모리당 1회 호출 + 관대한 sleep.
                if mode == "synopsis" and sleep_s > 0:
                    time.sleep(sleep_s)
        finally:
            conn.close()
    finally:
        _release_lock(lock)
    _debug(f"cr_backfill mode={mode} {counts}")
    return counts


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="MV Contextual Retrieval backfill.")
    ap.add_argument("--cr-backfill", action="store_true",
                    help="stale corpus_generation 메모리에 ctx 임베딩 채움")
    ap.add_argument("--mode", choices=list(BACKFILL_MODES), default="title")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.0,
                    help="synopsis 모드 메모리 간 sleep(초) — Gemma rate 보호")
    args = ap.parse_args(argv)

    if not args.cr_backfill:
        ap.error("--cr-backfill 플래그가 필요합니다")

    counts = cr_backfill(
        args.mode, limit=args.limit, dry_run=args.dry_run, sleep_s=args.sleep
    )
    if counts.get("lock_busy"):
        print("cr-backfill: 인덱서 락 busy — 다른 인덱서 실행 중, 나중에 재시도하세요")
        return 0
    tag = "[dry-run] " if args.dry_run else ""
    print(f"{tag}cr-backfill mode={args.mode}: candidates={counts['candidates']} "
          f"processed={counts['processed']} unreadable={counts['skipped_unreadable']} "
          f"stale={counts['skipped_stale']} failed_embed={counts['failed_embed']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
