# goal.md — gbrain 차용 1순위 2건: Contextual Retrieval + Eval Gate

> 작성 2026-06-17 · 대상 `~/my-folder/apps/mindvault-v3` (v3.8.5, HEAD a1a979e)
> 짝 문서: [plan.md](./plan.md) · [status.md](./status.md) · [test.md](./test.md)
> 모든 코드 참조는 검증된 `file:line` (status.md §검증로그가 추적)

## 0. 한 줄 목표

gbrain(Garry Tan, Postgres-native 14.6만 페이지 brain)과 MV v3를 9단계로 비교한 결과 **MV가 베껴 명백히 이득인 2건** — ① **Contextual Retrieval(CR)**: 검색 정확도, ② **Eval Gate**: 회귀 측정 — 을 MV 핵심 원칙(CC 내부 전용·로컬 MLX zero-cost·사용자 행동 제로·점진적 가치)을 **하나도 깨지 않고** 이식한다. 이 문서는 그 2건의 목표·범위·제약·성공기준·비범위를 못박는다.

## 1. 배경 — 왜 이 2개인가

| 비교 단계 | gbrain 우위 | MV 차용가치 | 판정 |
|---|---|---|---|
| 3. 임베딩 | **CR** (청크에 synopsis 선붙여 재임베딩) | 가볍게 이식, 검색정확도 직결 | **베낀다 (1순위)** |
| 9. 평가 | **eval gate** (qrels + P@k/R@k + 회귀 baseline) | MV "측정 artifact"(66.3% vs 옛 2.6%/79% 혼란) 구조적 해결 | **베낀다 (2순위)** |
| 4. graph(relational) arm | 검색력↑ | **제외** — 회수메모리 `project_mindvault`: 그래프는 Graphify 의도적 분리(회수 scope 오염 방지) | 비범위 |
| 8. 합성+gap | 출력품질↑ | **제외** — MV는 Opus와 페어 → 미리 합성은 토큰 중복 | 비범위 |

## 2. MV 코드 실측 (문서·계획의 grounding 기준점)

> 직전 4개 매핑 에이전트 + 직접 grep 검증. 추정 아님.

**임베딩/인덱싱 (Feature A 대상면)**
- 메모리 임베딩 단위 = **파일 전체**. 메모리당 **2개 벡터**: `body` + frontmatter `description`. **청킹 없음.** (`src/memory_indexer.py:443-498`)
- 저장 = **일반 SQLite BLOB(float32 1024dim) + numpy** — sqlite-vec 아님. macOS 시스템 Python이 `enable_load_extension` 미지원이라 의도적. (`src/indexer.py:210-211`, `src/memory_indexer.py:5-6`) ⚠️ CLAUDE.md "sqlite-vec" 표기는 **부정확** — 본 문서는 코드 기준.
- 스키마: `memories_vec(rowid PK, path, kind TEXT 'body'|'description', embedding BLOB)` (`src/indexer.py:247-253`). `SCHEMA_VERSION=3` (`src/indexer.py:53`), 마이그레이션 훅 `_migrate_schema` (`src/indexer.py:274`).
- 임베딩 서버 Arctic-ko v2.0 KO, `localhost:8081`, 1024dim, `kind=query`엔 `"query: "` prefix·`passage`엔 무prefix, 토큰캡 8192(~32K자), `EMBED_TIMEOUT=5s`. (`scripts/arctic_ko_server.py`)
- 검색 `recall_memory(query, top_k, score_threshold, raw_cosine_min)` (`src/memory_search.py:550`): FTS5 + `_vec_top_k`(전체 벡터 numpy 로드, `:291-360`) → RRF(`RRF_K=60`) → raw cosine 절대 게이트(`DEFAULT_RAW_COSINE_MIN=0.32`, hook hinted 0.27) → `DEFAULT_TOP_K=1`. 상수 `DESCRIPTION_WEIGHT=1.5`, `DEPRECATED_DECAY=0.3` (`src/memory_search.py:40-57`).
- 증분 인덱싱 mtime 기반 `incremental_index` (`:379`), 전체 `full_rebuild` (`:510`). embed 실패 시 해당 파일 skip·mtime 미갱신 → 다음 run 재시도(defer 패턴).
- 회수 hook `HARD_TIMEOUT_MS=400ms`, `MIN_PROMPT_LEN=4` (`hooks/memory-recall.py:49`).

**평가 (Feature B 대상면)**
- `src/self_eval.py`(1359줄): **운영 텔레메트리** 분석만 — hit_rate(`picked>0`), internal_effort, FP(negative-cue regex), self-affirming, **recall_utilization 게이트(strict cited ≥0.15, judged≥30)** (`:1082-1122`). **labeled corpus·P@k/R@k·회귀 baseline·CI 없음.**
- `metrics.jsonl` 라이브 텔레메트리 필드: `ts,kind,query_len,elapsed_ms,picked,max_score,raw_top1_cosine,raw_min,has_hint,intent,intent_matched,recalled_ids`. → **`recalled_ids`가 코퍼스 시드 재료**(단 "회수된 것"이지 "정답"이 아님 — 사람 확인 필요).
- `src/eval_top3_domain.py`: 수기 10쿼리, **수동 육안 채점**, 라벨 없음.
- `tests/benchmark_search.py`: **지연** 벤치(avg<150ms,p95<200ms) — 품질 아님.
- pytest **816개** 수집(`pytest --collect-only`; `def test_` grep은 814이나 클래스/파라미터화 2건 추가), `tests/conftest.py` hermetic 격리. **`.github/workflows`·Makefile·pyproject 테스트설정 없음**(로컬 전용).

## 3. 목표 정의

### Feature A — Contextual Retrieval (CR)
**정의**: body를 임베딩하기 전에 그 메모리의 **맥락 한 줄(name + synopsis)** 을 선붙여 임베딩한다. 짧은/coined-name 메모리가 문서 맥락을 벡터에 담아 리터럴로 안 잡히던 회수를 잡는다. (gbrain `contextual-retrieval-service.ts` 포팅, Anthropic Contextual Retrieval 패턴.)

**MV 적응 (gbrain과 다른 결정 + 이유)**
- gbrain은 청크 단위지만 **MV는 파일 전체 단위** → CR 대상은 **body 임베딩**. 청킹 도입 안 함(비범위, §4).
- **3-tier 모드** 이식: `off`(기본) / `title` / `synopsis`.
  - `title` = `<context>{name}\n{description}</context>\n{body}` 임베딩. **LLM 0회** — MV frontmatter `description`이 이미 사람이 쓴 synopsis라 무료 이식 가능(gbrain title-tier 대응).
  - `synopsis` = `description`이 빈약하거나 추가 맥락 필요 시 **로컬 Gemma 12B(`localhost:8080`)** 가 1줄 생성. gbrain은 Haiku(클라우드)지만 MV는 **zero-cost 원칙상 클라우드 금지** → 로컬 Gemma.
- **비용은 index-time 전액** — 회수 query-time 지연 예산(p95<400ms)을 1ms도 안 늘림. synopsis·contextual 임베딩은 인덱싱/백필 때 1회 생성·저장, 회수 땐 기존 벡터 조회만. 쿼리는 wrapper 없이 clean 임베딩(asymmetric).
- **원본 불변 invariant**(gbrain D20-T1 이식): `memories_fts.body`·스니펫·원 `embedding`은 그대로. wrapped 문자열은 **contextual 임베딩 생성에만** 사용, 별 컬럼에 저장.
- `off` 모드 = 신컬럼 미사용 → 기존 동작 바이트 동일(회귀 0, 머지 조건).
- **graceful degradation**: Gemma 다운/거부/빈응답 → `synopsis`→`title` tier 강등 → 그래도 빈 맥락이면 raw. 인덱싱 절대 중단 안 함(기존 embed-defer·`test_embed_resilience` 정신 계승).
- **corpus_generation 해시**(gbrain 이식): `sha256(mode|synopsis_prompt_ver|gemma_model|wrapper_ver|embed_model)[:16]` 를 메모리별 저장 → 입력 바뀌면 stale 감지·재임베딩. mtime만으로 못 잡는 "모드/프롬프트 변경 재임베딩"을 닫음.

### Feature B — Eval Gate
**정의**: `query → 기대 메모리(name)` 라벨 코퍼스로 회수를 돌려 **P@k/R@k/MRR**를 계산하고, 저장된 **baseline 대비 회귀 시 비영 exit**. CR 같은 검색 변경을 머지 전 "정확도 떨어뜨렸나"로 자동 판정한다. → **Feature A의 측정 계기(A4 증명 도구)이므로 A보다 먼저 구축.**

**MV 적응 (기존 자산 재사용 + 규모 적정화)**
- 기존 `self_eval.py`·`metrics.jsonl`·`conftest` 격리 **재사용**, 중복 구현 금지. 추가하는 것: (a) qrels 라벨 코퍼스, (b) P@k/R@k/MRR 스코어러, (c) baseline 회귀 게이트, (d) pytest 배선.
- qrels 스키마는 gbrain 이식하되 **MV는 `name` slug 단일**(source_id/slug 페어 불필요) → `relevant: [name...]`.
- 규모: gbrain 240페이지 Opus 코퍼스가 아니라 **MV 수백 메모리** → 케이스 ≥20 수기 시드 + `metrics.jsonl recalled_ids` 후보 보강(사람 확인). 10만 스케일 가정 이식 금지.
- 게이트 = **correctness(절대 임계) + regression(baseline 비교)** dual. baseline은 repo 버전관리 파일.
- **CI는 GitHub Actions 강제 안 함** — MV는 `.github` 없는 로컬 전용. 게이트 = `pytest`(slow 마커) + `python -m src.eval_gate ...` CLI + (선택) pre-push 훅. 외부 서비스 의존 0(원칙 1·2).
- **결정성**: eval은 hook이 아니라 `recall_memory`를 직접 호출(intent 분류기 우회) → numpy 코사인 결정적 → 2회 실행 동일(B5, 옛 measurement artifact 재발 차단).

## 4. 비범위 (명시적 제외)

- **graph/relational arm** — `project_mindvault` "Graphify 의도적 분리" 결정과 충돌 → 제외.
- **합성(synthesis) 레이어** — Opus가 합성 → 토큰 중복 → 제외. (gap 고지 1줄은 별도 백로그, 본 범위 밖.)
- **청킹(chunking)** — MV는 파일 전체 임베딩 유지. 단 **장문 메모리 임베딩 절단**(Arctic-ko 8192토큰캡; 예: `project_mindvault` 53K자 절단, 깊은 사실 임베딩 누락)은 **알려진 한계로 기록**하되 CR이 직접 해결하진 않음(synopsis가 문서 맥락은 보강하나 절단은 별건). 청킹은 후속 과제.
- **Postgres/PGLite 마이그레이션** — SQLite BLOB+numpy 유지 → 제외.
- **자동 atom 추출(continuous LLM ingest)** — close-session+사람 검토 게이트 유지 → 제외.
- **클라우드 LLM/임베딩**(Haiku 등) — 제외(원칙 2). synopsis는 로컬 Gemma만.
- **공개 서비스화** — v1 공개 실패 + CC 내부 전용 → 제외.

## 5. 성공 기준 (측정 가능)

### Feature B — Eval Gate (먼저)
- B1. `query→relevant(name)` qrels 코퍼스 파일(`evals/recall_qrels.json`)이 존재·스키마 검증, 케이스 **≥20**, 도메인 분산(라벨 태깅).
- B2. eval 러너가 **P@5·R@5·MRR·first_relevant_hit·expected_top1** 산출, JSON 리포트 + baseline 파일 생성.
- B3. 게이트가 baseline 대비 허용폭 초과 회귀 시 **exit 1**, 동률/개선 시 **exit 0**, 사용오류 **exit 2** — 양방향 테스트 증명.
- B4. pytest(slow 마커)·CLI 한 줄로 실행, 기존 816 테스트와 격리 공존, Arctic-ko 미가동 시 graceful skip.
- B5. **결정성**: 동일 코퍼스·인덱스에서 2회 실행 결과 동일(`recall_memory` 직접호출, 시드 고정).

### Feature A — CR (B로 측정)
- A1. 모드 `off`(기본)에서 **기존 816 pytest 100% green**, 인덱스/검색 동작 바이트 동일(회귀 0).
- A2. 모드 `synopsis`/`title`에서 인덱싱이 메모리당 contextual 임베딩을 1회 생성·`index.db` 신컬럼 저장, Gemma 다운 시 tier 강등→raw 폴백(인덱싱 무중단)이 테스트로 증명.
- A3. **회수 query-time 지연 회귀 0** — CR 켜도 `benchmark_search.py` p95 증가 없음(synopsis는 index-time only).
- A4. Feature B 코퍼스에서 `synopsis`(또는 `title`)가 `off` 대비 **P@5·R@5 비열등(≥)** + 최소 1개 coined-name/짧은 메모리 케이스 회수 성공 전환(개선 ≥1건 입증). 악화 시 기본 비활성 유지 + 사유 기록.

### 문서 품질 (이 작업의 1차 산출물)
- D1. 4문서가 실제 코드 `file:line`에 grounding — **존재하지 않는 파일/함수/컬럼 참조 0**.
- D2. 적대적 검증(다중 리뷰어)에서 **기술오류·내부모순·실현불가 0** 수렴, status.md에 라운드별 로그.

## 6. 리스크 (plan.md에서 완화 상술)
- R1. synopsis가 검색 악화 → 모드 옵트인 + B 게이트로 before/after 강제측정, 악화 시 비활성.
- R2. Gemma synopsis 지연/실패가 인덱싱 취약 → index-time 배치 + 타임아웃 + tier 강등 + raw 폴백 + 재개가능 백필.
- R3. 라벨 코퍼스 과적합(현 알고리즘에 맞춘 정답) → `recalled_ids` 실트래픽 보강 + 케이스 출처 태깅 + 정기 갱신.
- R4. `index.db` 스키마 변경이 호환성 깸 → `_migrate_schema`로 ALTER ADD(nullable) + `off` 시 신컬럼 미사용 + `full_rebuild` 경로.
- R5. 문서-코드 drift → status.md 반복 검증 루프(본 작업 종료조건).

## 7. 종료 조건 (이 작업)
§5 D1·D2 충족: 4문서가 실제 코드에 grounding되고 적대 검증에서 오류 0으로 수렴, status.md에 라운드별 기록. (코드 구현은 plan.md가 정의하는 후속 — 본 산출물은 **검증된 4문서**.)
