---
name: handoff-sprint-next-7-scan-cache
description: V3-NEXT-IMPROVEMENTS #7 — turns_cache.py 신규, jsonl mtime 기반 incremental sqlite 인덱스. self_eval --use-cache 옵션 (opt-in). 운영 latency 2.42s → 0.47s (5배 단축), 결과 동등 검증.
---

MindVault v3 → 차기 보강 #7 — scan latency 캐시 빌드 로그

## 요약

V3-NEXT-IMPROVEMENTS #7 해결. SPRINT-15-BUILD-LOG 미해결 4번. self_eval 이 매 호출 시 모든 jsonl (운영 190개) 을 재 parsing 해 답답한 latency. turns_cache 모듈로 mtime 기반 incremental sqlite 인덱스 도입.

master HEAD `b431e16` (NEXT-6 slug conflict) 기준 worktree `worktree-next-7-scan-cache`.

## 자율 결정 사유

- **opt-in `--use-cache` 플래그** — 기본 동작 (직접 parsing) 보존. 캐시 로직 버그·schema 손상이 있어도 형이 플래그 빼면 즉시 기존 경로. 운영 검증 누적 후 default-on 결정 가능.
- **sqlite 캐시 (별도 DB `turns_cache.db`)** — 기존 `~/.claude/mindvault-v2/index.db` 와 분리. memory 인덱스(FTS5+vec) 와 self_eval 캐시는 schema·용도 완전 다름, 한 DB 에 합치면 마이그레이션·잠금 충돌 위험. 별도 파일이 안전.
- **mtime_ns 단위 변경 감지** — Python `Path.stat().st_mtime_ns` 가 1ns 정밀도. 운영 jsonl 은 SessionEnd 한 번에 새 jsonl 만들거나 append → mtime 항상 갱신. 변경 0인 jsonl 은 skip.
- **jsonl 삭제 처리 안 함** — 운영 jsonl 는 누적만 됨 (claude 가 새 sessionId 마다 새 파일). 삭제 시나리오 미존재 → 처리 안 함이 단순. 만약 형이 수동 삭제하면 캐시에 stale row 남지만 since_unix 필터링 +최신 jsonl 이 우선이라 분석 결과에 영향 없음. 수동 cleanup 은 `rebuild` CLI 로 가능.
- **turns_cache 가 self_eval 의 load_turns + iter_session_jsonl_paths 재사용** — 본질적 turn 추출 로직 (system-reminder 필터, tool_use 추출, hook artifact 제외) 은 self_eval 한 곳에만 있어야 정합 유지. 캐시는 그 결과를 저장만.
- **tool_uses 직렬화는 JSON** — sqlite 에 list 직접 저장 불가. JSON 텍스트로 직렬화. 평균 turn 당 0~2 tool_use 라 cost 무시 가능.
- **CLI `refresh / rebuild / stats` 서브** — 형이 운영 상태 진단하기 좋게. `stats` 가 indexed_jsonl_files / turns 카운트 + db_size 표시.

## 변경 상세

### A. `src/turns_cache.py` 신규 (~200 lines)

```python
open_cache(db_path) -> sqlite3.Connection
  # jsonl_state(jsonl_path PK, mtime_ns, last_indexed_at) +
  # turns(jsonl_path, ts_unix, role, text, tool_uses) +
  # idx_turns_ts on (ts_unix) + idx_turns_path on (jsonl_path)

refresh_cache(projects_root, db_path, full=False) -> dict
  # full=True 면 turns + jsonl_state 모두 truncate 후 rebuild
  # 아니면 mtime 변경분만 재 parse, 변경 없는 jsonl 은 skip
  # 반환: {scanned, reindexed, skipped, elapsed_ms}

get_turns_since(since_unix, projects_root, db_path, auto_refresh=True) -> list[dict]
  # auto_refresh=True 면 호출 직전 refresh_cache 수행
  # since_unix 필터는 SQL WHERE 으로 위임 (인덱스 활용)

cache_stats(db_path) -> dict
  # DB 존재·크기·indexed_jsonl_files·indexed_turns 진단

main()
  # CLI: refresh / rebuild / stats sub
```

### B. `src/self_eval.py` 통합

- `analyze_recent` 에 `use_cache=False` 파라미터 추가.
- use_cache=True 면 turns_cache.get_turns_since 호출. 실패 시 graceful 폴백 (debug log + 직접 parsing).
- main argparse 에 `--use-cache`, `--rebuild-cache` 플래그.
- `--rebuild-cache` 단독 사용 시 main 진입 직후 full=True refresh 수행.

## 측정 데이터

### 단위 테스트

```
tests/test_turns_cache.py: 7/7 PASS (0.03s)
  test_first_refresh_indexes_all
  test_second_refresh_skips_unchanged
  test_mtime_change_triggers_reindex
  test_get_turns_returns_indexed
  test_get_turns_since_filter
  test_full_rebuild_clears_old
  test_cache_stats
```

### 전체 회귀

```
232/234 PASS (test_install_uninstall 제외, 99s)
2 fail = test_schema_v2.* — master HEAD `b431e16` 동일 pre-existing.
```

### 운영 latency 실측 (190 jsonl, 1518 recall events)

| 시나리오 | 실측 |
|---|---|
| `turns_cache.py rebuild` (cold) | **1.30s** (190 jsonl 전체 인덱싱) |
| `self_eval --hours 168 --use-cache` (warm, 2차) | **0.47s** |
| `self_eval --hours 168` (캐시 없이, 기존) | **2.42s** |

- 결과 동등성 검증: total_recalls=1518, hit_rate=0.870 — 양쪽 동일.
- 캐시 hit 시 **5.1배 단축** (2.42s → 0.47s). brief 의 50s baseline 은 더 큰 jsonl 모집단 시나리오 — 본 실측은 현 운영 환경 기준.
- 첫 빌드 cost (1.3s) 는 1회만. 이후 새 jsonl 만 처리 → 거의 0 cost 누적.

## 안전 정책 준수

- `indexer.full_rebuild()` (memory 인덱스) 호출 없음 — 별도 DB.
- Sprint 10 트랜잭션 패턴 무관 (캐시는 별도 모듈).
- BGE plist / `bge_m3_server.py` 무변경.
- launchctl 서비스 (`arctic-ko-mlx`, `gemma-mlx`, `mv2-env`) 무관.
- self_eval 기존 동작 보존 (--use-cache 옵션). 캐시 실패 시 graceful 폴백.
- worktree 격리.

## 미해결 / 후속 정리

- **scan_self_affirming_memories 캐시 활용** — 본 sprint 는 analyze_recent 의 all_turns 로딩만 캐시 적용. scan_self_affirming_memories 도 비슷한 pattern 으로 캐시 활용 가능. 운영 누적 후 latency 측정 후보.
- **default-on 전환** — opt-in 검증 1주일 누적 후 `--use-cache` 기본 ON 으로 전환 검토. 그러면 형이 CLI 옵션 외울 필요 없음.
- **launchctl 정기 refresh 데몬** — 현재 self_eval 호출 시점에 lazy refresh. 정기 background refresh (cron / launchd) 로 cold path 도 거의 0 가능. 운영 빈도 보고 결정.

## 변경 파일

```
src/turns_cache.py                                    | 신규 (~205 lines)
src/self_eval.py                                      | +35 -5 (use_cache flag + analyze_recent 통합)
tests/test_turns_cache.py                             | 신규 (~135 lines)
handoff/SPRINT-NEXT-7-SCAN-CACHE-BUILD-LOG.md         | 신규
```
