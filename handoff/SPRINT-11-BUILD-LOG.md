---
name: handoff-sprint11-build-log
description: Sprint 11 build log — hook 회수 품질 개선. SNIPPET_CHARS 160→600 발췌 확장 + query keyword 매치 밀집도 sliding window broad word filter, _expand_wikilinks raw_cosine 게이트(factor 0.75) 무관 1-hop 노이즈 차단, MV3_EXTRA_MEMORY_DIRS env var으로 handoff 인덱싱 scope 추가
---

MindVault v3 Sprint 11 — hook 회수 품질 개선 빌드 로그

## 요약

Sprint 10 직후 형 실전 테스트로 드러난 3가지 결함(A: 발췌 길이 부족, B: wikilink 무관 메모리 노이즈, C: handoff/ indexing scope 부재) 모두 처리. master HEAD `6cb6818` 기준.

- `src/memory_search.py` — A(SNIPPET_CHARS 160→600 + query-aware sliding window) + B(_vec_top_k raw_map 전체 path 확장 + wikilink target raw_cosine 게이트)
- `src/memory_indexer.py` — C(env var `MV3_EXTRA_MEMORY_DIRS` 추가 indexing scope)
- `hooks/memory-recall.py` — C 일관 적용(_mtime_changed가 extra dir도 watch)
- 산출물: dev → `~/.claude/scripts/mindvault/` sync 완료. production hook MD5 일치 확인.

## 변경 상세

### A. 발췌 확장 (`src/memory_search.py`)

- `SNIPPET_CHARS = 160 → 600`. 4x 확장.
- `_query_window(body, query, char_budget)` 신규: query 단어 매치 위치 밀집도 max 지점 ±half window 발췌. 단순 first-hit은 "mindvault" 같은 broad keyword가 헤더에 박혀있으면 무의미 → 매치 밀집 cluster 기반.
- `BROAD_WORD_FREQ_LIMIT = 5`: 본문에 5회 초과 등장하는 query word는 broad/generic으로 분류해 매치 후보에서 제외. 메모리 이름과 동일한 단어(예: "mindvault")가 본문 헤더·전반에 박혀 specific keyword 한 번 매치를 묻는 노이즈 회피.
- 다 broad word인 경우엔 graceful degrade: 가장 freq 낮은 단어만 채택해 적어도 위치 hint 유지.
- 동률 동시 시 더 늦은(최신 sprint일 가능성) 위치 선호.
- query word 없거나 매치 0건이면 `body[:SNIPPET_CHARS]` fallback — 이전 동작 보존.

### B. wikilink expansion 게이트 (`src/memory_search.py`)

- `_vec_top_k`: raw_map을 `limit`과 무관하게 **전체 path × kind**의 최대 cosine 보유하도록 변경. 메인 recall_memory 게이트는 path-단위 max 사용이라 동작 동일. wikilink expansion 게이트가 top-K 밖 target도 정확히 cosine 평가 가능.
- `_expand_wikilinks(conn, results, raw_cosine_map, raw_cosine_min, query=None, max_expansion=...)`: signature 확장.
  - `WIKILINK_GATE_FACTOR = 0.75`. target의 raw_cosine이 `raw_cosine_min × 0.75` 미만이면 expand 차단.
  - 예: 기본 `raw_cosine_min=0.40` → wikilink 게이트=0.30. hint 있으면 `raw_cosine_min=0.32` → 게이트=0.24 (더 너그러움 — 형 회수 의도 명확할 때만 wikilink 노이즈 trade-off).
  - target의 snippet 생성에도 query 전달 (sliding window 일관).
  - 차단 시 debug.log에 `wikilink gate block slug=... target_raw=... gate=...` 기록.

### C. handoff/ indexing scope (`src/memory_indexer.py` + `hooks/memory-recall.py`)

- env var `MV3_EXTRA_MEMORY_DIRS=path1:path2` (Unix `PATH` 스타일 separator) 추가.
- `_extra_memory_dirs()` 신규: env 파싱 → `Path.expanduser()`.
- `incremental_index()`이 `DEFAULT_MEMORY_DIRS + _extra_memory_dirs()` 처리.
- `hooks/memory-recall.py`의 `MEMORY_DIRS`도 동일하게 env 읽음 → `_mtime_changed()`가 extra dir mtime도 watch.
- `_spawn_reindex`는 부모 env 보존하므로 indexer 자체 env 일관.
- 설치/적용: 형 shell rc에 `export MV3_EXTRA_MEMORY_DIRS=...` 1회. env 설정 후 첫 회는 mtime 변경 없어 hook trigger 안 됨 → `python3 ~/.claude/scripts/mindvault/memory_indexer.py` 수동 1회 실행 필요.

## 측정 데이터

### A. 발췌 비교 (형 query 재현)

query: `mindvault dbe 모델 말고 이름이 기억 안나는데 다른 모델로 바꿔서 그 성능이 얼마나 좋아 졌었지?`

| 항목 | Baseline (HEAD 6cb6818) | Sprint 11 |
|---|---|---|
| picked 건수 | 2 (project-mindvault + wikilink-1hop noise) | 1 (project-mindvault만) |
| 발췌 길이 | 159자 | 600자 |
| 발췌 도달 깊이 | repo 경로 + DB 위치 헤더 | Sprint 6 결정사항 (precision@3 23%→50%, 인덱스 커버리지 3대 문제) 까지 |
| wikilink 노이즈 | feedback-transcript-lone-surrogate (raw 0.25, 무관 도메인) | 차단됨 (gate 0.30 vs target 0.25) |
| hook elapsed_ms | ~72ms | 62-66ms (캐시 hit) |

### B. wikilink 게이트 종합 (raw_cosine_min=0.32 / 0.40)

| query | top-1 (raw) | wikilink expand 살아남음 |
|---|---|---|
| `grammar saas 영어 선생님 반복 학습` | project-grammar-saas (0.598) | user-english-teacher-background (0.589), feedback-repetition-learning (0.532) — 동일 도메인 cluster 보존 |
| `scan natural language scanner cli` | scan-natural-language (0.619) | scanner-cli (0.473) 보존 |
| `html output default 비디오 출력` | anthropic-html-video (0.581) | html-output-default (0.539) 보존 |
| `mindvault sprint 진행` (default 0.40) | project-mindvault (0.604) | feedback-transcript-lone-surrogate (raw 0.25 < gate 0.30) **차단** |
| `mindvault sprint 진행` (hinted 0.32) | project-mindvault (0.604) | feedback-transcript-lone-surrogate (raw 0.25 > gate 0.24) 통과 — hinted 의도 trade-off |
| `안녕하세요 오늘 날씨` | (no hits) — raw cosine 게이트 차단 정상 | — |
| `youtube 영상 만들어줘` | 유튜브 롱폼 파이프라인 (0.457) | 단일 hit, 다른 도메인 정상 |

### C. indexing scope 확장

- env var 적용 + indexer 수동 1회 실행 후: handoff/ 8개 .md 모두 indexed.
- 형 query에서 handoff 후보 rank: V3-PLAN(5), SPRINT-10-BUILD-LOG(6), SESSION-CHECKPOINT(9), SPRINT-10-BRIEF(10). raw 0.39-0.41.
- top-K=1 정책상 project-mindvault(rank 1, raw 0.45)이 여전히 picked. handoff/ 콘텐츠는 frontmatter 부재로 description 임베딩 없음 → cosine 분포 낮음.
- 결론: C의 본질은 **인덱싱 scope 확장** (성공). 매번 hit 보장은 X — handoff/*.md에 frontmatter 추가하면 description-weighted 매칭 개선 가능 (별도 작업).

### DB 카운트

| 테이블 | Before (master HEAD) | After (Sprint 11 + handoff env) |
|---|---|---|
| memories | 106 | 114 (+8 handoff) |
| memories_vec | ~209 | 217 (+8 body-only handoff) |
| memories_fts | 106 | 114 |
| sessions | 178 | 178 (보존) |

### 회귀 검증

- `tests/test_memory_search.py`: 11/11 PASS
- `tests/test_memory_indexer.py`: 15/16 PASS (`test_embed_timeout_returns_none` fail — Sprint 11 무관, master HEAD에서도 동일 fail. production embed_cache의 "hello" entry가 mock urlopen 우선 hit하는 test isolation 결함. 별도 작업으로 정리 필요).
- `tests/test_memory_hook.py`: 5/5 PASS
- 동시성 스트레스 (50 hook + 1 indexer 동시): `database is locked` / `cache put fail` / `FATAL` 0건. Sprint 10 트랜잭션 패턴 보존.
- 잡담 차단·도메인 hit·다른 도메인 hit 모두 baseline 동작 보존.

## 미해결 / Sprint 12 후보

1. handoff/*.md frontmatter 추가 — `name` + `description` 추가하면 description 임베딩 가능해져 cosine 향상.
2. `test_embed_timeout_returns_none` test isolation 결함 — production DB embed_cache 분리 또는 cache mock.
3. duplicate memory dedup — `-Users-yonghaekim/` vs `-Users-yonghaekim-my-folder/` 두 디렉토리에 동명 `project_mindvault.md` 양쪽 indexed. cosine 더 높은 쪽이 picked되지만 잡혀야 할 콘텐츠가 늦게 작성된 쪽에만 있으면 누락 가능.
4. install.sh에 `MV3_EXTRA_MEMORY_DIRS` 안내 (현재는 형 shell rc 수동 설정 필요).

## 변경 파일

```
hooks/memory-recall.py     | +9
src/memory_indexer.py      | +24
src/memory_search.py       | +83
handoff/SPRINT-11-BUILD-LOG.md (신규)
```
