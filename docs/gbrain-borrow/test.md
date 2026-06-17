# test.md — 검증 계획

> 짝: [goal.md](./goal.md) · [plan.md](./plan.md) · [status.md](./status.md)
> 각 케이스는 goal §5 성공기준에 역추적된다. 실행 결과는 status.md에 기록.

## 0. 테스트 철학 (MV 관례 계승)

- **TDD**: 각 Phase는 실패 테스트 → 구현 → green.
- **격리**: `tests/conftest.py`의 hermetic env 격리 재사용 — 모든 `MV3_*` 경로 tmp 리다이렉트, 운영 `index.db`/`metrics.jsonl` 불침범.
- **결정성 우선**: 임베딩/LLM 불요한 순수 로직은 mock/합성 데이터로 결정적 테스트. Gemma/Arctic-ko 의존 테스트는 `@pytest.mark.slow` + 미가동 시 `pytest.skip`.
- **회귀 게이트**: 기존 **816 pytest 100% green** 이 모든 Phase의 통과 전제.
- **Gemma 자동 위임**: 합성 더미 메모리/쿼리 코퍼스 생성 같은 bulk 텍스트 작업은 `gemma-worker`로 위임(CLAUDE.md 규약), 최종 라벨 정답은 사람 확인.

## 1. 테스트 매트릭스 (기준 ↔ 케이스)

| ID | goal 기준 | 대상 | 종류 | 결정적? | 의존 |
|---|---|---|---|---|---|
| T-B1 | B2 | `ranking_metrics` P@k/R@k/MRR/first_hit/expected_top1 | unit | ✅ | 없음 |
| T-B1b | B2 | 러너 리포트+baseline에 5개 메트릭키 생성 | unit(mock recall) | ✅ | 없음 |
| T-B2 | B3 | `eval_gate.evaluate` pass/fail 양방향 | unit | ✅ | 없음(합성) |
| T-B3 | B1 | qrels 스키마 검증 | unit | ✅ | 없음 |
| T-B4 | B5 | 러너 2회 실행 동일 | integration | ✅(시드고정) | Arctic-ko |
| T-B5 | B4 | pytest 배선·Arctic-ko 미가동 skip | integration | ✅ | 조건부 |
| T-B6 | B3 | CLI exit 0/1/2 | e2e | ✅ | Arctic-ko |
| T-A1 | A1,R4 | v3→v4 마이그레이션 멱등·데이터 보존 | unit | ✅ | 없음 |
| T-A2 | A2,R2 | 인덱서 CR 생성·Gemma 폴백·tier 강등 | unit | ✅(Gemma mock) | 없음 |
| T-A3 | A1,A3 | 검색 off 회귀 0 + ctx 폴백 | unit/integration | ✅ | 조건부 |
| T-A4 | A2 | CR 백필 stale 감지·재개·dry-run | unit | ✅ | 없음(mock) |
| T-A5 | A3 | query-time 지연 회귀 0 | bench | ⚠️통계 | Arctic-ko |
| T-A6 | A4 | synopsis vs off A/B 게이트 판정 | e2e | ✅(고정코퍼스) | Gemma+Arctic |
| T-D1 | D1 | 문서 코드참조 존재성 | doc-lint | ✅ | repo |
| T-D2 | D2 | 적대검증 오류 0 수렴 | review | — | 리뷰어 |

## 2. 케이스 상세

### T-B1 — ranking_metrics (순수) — B2 메트릭 함수 전수
- `recall_at_k(["a","b","c"], {"b"}, k=5) == 1.0`; `… k=1 == 0.0`.
- `precision_at_k(["a","b"], {"a"}, k=2) == 0.5`.
- `reciprocal_rank(["x","y","z"], {"z"}) == 1/3`; relevant 없음 → `0.0`.
- `first_relevant_hit(["a","b"], {"b"}) == 0`(top-1 아님); `first_relevant_hit(["b","a"], {"b"}) == 1`.
- `expected_top1_hit(["b","a"], "b") == 1`; `… "a") == 0`; `expected_top1` 없음(None) → `None`(분모 제외).
- 경계: `k > len(retrieved)`, `relevant` 다중, `retrieved` 빈 리스트, 중복 name.
- `score_corpus` 집계가 per-query 평균과 일치, 출력 키 5종(`mean_recall_at_5,mean_precision_at_5,mrr,first_relevant_hit_rate,expected_top1_hit_rate,n`) 존재.

### T-B1b — 러너 리포트/baseline 생성 (B2, mock recall) ★
- `recall_memory`를 mock(쿼리→고정 retrieved name 리스트)으로 주입 → `run_corpus`가 임베딩 없이 결정적 동작.
- 생성된 리포트 dict + baseline JSON에 **5개 메트릭 키 전부** + `per_query` + `n` 존재 확인(goal B2의 "JSON 리포트 + baseline 파일 생성" 직접 검증).
- baseline 파일에 `thresholds`·`git_commit`·`generated_at`·`k` 필드 존재(plan §2.1 스키마 일치).

### T-B2 — eval_gate.evaluate (순수, 양방향) ★핵심
- **pass**: current == baseline → `verdict=="pass"`, breaches 빈 리스트.
- **fail-correctness**: current.mean_recall_at_5 < min_recall_at_5 → breach 1건, `verdict=="fail"`.
- **fail-regression**: baseline 0.80 → current 0.74, max_recall_drop=0.03 → drop 0.06 > 0.03 → fail.
- **pass-improve**: current > baseline → pass(개선은 절대 fail 아님).
- **fail-closed**: per_query 예외 주입 → fail.

### T-B3 — qrels 스키마
- 유효 파일 로드 OK; `schema_version != 1` → 에러; `queries` 빈 배열 → 에러; `relevant` 빈/누락 → 에러; 중복 query_id 감지.

### T-B4 — 러너 결정성 (B5) ★핵심
- 동일 qrels·동일 index.db로 `run_corpus` 2회 → per_query `retrieved` 순위·메트릭 **완전 동일**.
- `recall_memory` 직접호출(intent 분류기 우회) 확인 — hook 비결정 요소 배제.

### T-B5 — 배선·skip
- `MV3_RUN_EVAL_GATE` 미설정 또는 slow 미선택 시 일반 run에서 제외.
- Arctic-ko 8081 미가동 → `pytest.skip(reason)`, 실패 아님.
- 816 기존 테스트와 동시 실행 시 상호 오염 0(격리).

### T-B6 — CLI exit 코드
- 합성 baseline(아주 높은 임계)로 `python -m src.eval_gate …` → **exit 1**.
- baseline == current → **exit 0**.
- 없는 qrels 경로 → **exit 2**.
- `--json` 출력이 valid JSON·`verdict` 키 포함.

### T-A1 — 마이그레이션 (A1, R4)
- v3 DB 픽스처 → `_migrate_schema` → `PRAGMA user_version`/SCHEMA_VERSION==4, 신컬럼 4개 존재.
- **멱등**: 2회 실행 에러 없음.
- 기존 `memories`/`memories_vec` 행 보존(개수·embedding 바이트 동일).
- 신규 DB(`CREATE TABLE`)에도 신컬럼 존재.

### T-A2 — 인덱서 CR (A2, R2) ★핵심
- `MV3_CR_MODE=off`: `embedding_ctx` NULL, `embedding`은 off 이전과 바이트 동일, `cr_mode=="off"`.
- `MV3_CR_MODE=title`: prefix `<context>{name}\n{description}</context>\n` 형태, `embedding_ctx` 채워짐, LLM 호출 0(Gemma mock 호출횟수 0 assert).
- `MV3_CR_MODE=synopsis` + Gemma mock **성공**: `cr_synopsis` 채워짐, `cr_mode=="synopsis"`.
- Gemma mock **거부/빈/타임아웃**: `cr_mode=="title"` 강등, 인덱싱 성공(예외 없음).
- Gemma mock **다운(연결거부)** + ctx 임베딩까지 실패: `cr_mode=="off"` 완전 폴백, 파일 정상 인덱싱(skip 아님).
- `corpus_generation` 16자, mode 바뀌면 값 변함.
- **원본 불변**: `memories_fts.body`·`embedding`·스니펫 경로 off와 동일.

### T-A3 — 검색 (A1, A3)
- `MV3_CR_SEARCH=0`(또는 미설정): `tests/test_memory_search.py` 전체 회귀 0(기존 assert 유지).
- on: `embedding_ctx` 채운 fixture → 해당 메모리 코사인이 ctx 기준 계산됨.
- on + `embedding_ctx` NULL 메모리: `COALESCE`로 raw `embedding` 폴백(누락 0).
- 쿼리 임베딩이 wrapper 미적용 clean인지 확인.

### T-A4 — 백필
- generation 불일치 메모리만 재처리(일치 메모리 호출 0).
- `--dry-run`: 쓰기 0, 대상 리스트만.
- 중단 후 재실행 → 이미 처리분 skip(재개·멱등).

### T-A5 — 지연 회귀 (A3) — 벤치
- `tests/benchmark_search.py` 를 off vs CR-on 두 번 → p95 차이가 노이즈 범위(예: ≤ +5ms 또는 baseline p95<200ms 유지).
- synopsis 생성이 검색 경로에 없음(인덱싱만)을 코드경로로도 확인.
- ⚠️ 통계적: 단일 실행 변동 큼 → n≥104 반복 기존 하니스 사용, 경향만 본다.

### T-A6 — A/B 게이트 판정 (A4) ★최종
- 고정 코퍼스로 off baseline 확정 → synopsis 백필 → 게이트 실행.
- 합격: `mean_recall_at_5(synopsis) ≥ mean_recall_at_5(off) - max_drop` AND coined-name 케이스(예 q-coined) 회수 성공 전환 ≥1.
- 결과(개선/동률/악화)를 status.md에 수치로 기록. 악화면 기본 off 유지 결정 + 사유.

### T-D1 — 문서 코드참조 doc-lint
- 4문서의 모든 `src/…:line`·함수명·컬럼명·상수를 repo에서 grep → **존재 확인**(존재성만; 라인은 drift 허용오차 ±소수).
- 존재하지 않는 참조 발견 시 즉시 수정(D1).
- 자동화: 간단 스크립트 또는 적대검증 리뷰어가 수행.

### T-D2 — 적대 검증 수렴 (D2)
- 다중 리뷰어(아래 §3)가 각 문서를 코드 대조로 채점 → 발견 결함을 status.md 라운드 로그에 기록 → 수정 → 재검증. **오류 0 라운드 2연속**까지 반복.

## 3. 적대 검증 절차 (오류 0 수렴 — 본 작업 종료조건)

문서는 코드가 아니므로 "테스트 통과"가 아니라 **코드 대조 검증**으로 닫는다. 라운드마다:

1. **정확성 리뷰어** — 모든 `file:line`·함수·컬럼·상수가 실제 코드와 일치하는가(T-D1).
2. **실현가능성 리뷰어** — 제안 변경이 실제 코드 구조에서 동작 가능한가(마이그레이션·폴백·검색경로가 실제 함수 계약과 맞는가).
3. **제약 리뷰어** — goal §3 원칙(로컬 zero-cost·CC 내부·index-time 지연·off 회귀 0) 위반 항목·회수메모리(`project_mindvault` graph 분리 등) 충돌 없는가.
4. **내부 일관성 리뷰어** — 4문서 상호 모순(Phase 번호·파일명·컬럼명·시퀀싱) 없는가.

각 리뷰어 발견 → status.md `## 검증 라운드 N`에 `[리뷰어] 결함 → 조치` 로 기록 → 수정 → 다음 라운드. **2연속 결함 0**이면 수렴 선언.

## 4. 실행 명령 (구현 후)

```bash
# 기존 회귀(항상)
pytest tests/ -q

# 신규 unit(결정적)
pytest tests/test_ranking_metrics.py tests/test_eval_gate.py tests/test_migration_v4.py \
       tests/test_cr_indexer.py tests/test_cr_search.py tests/test_cr_backfill.py -q

# eval 게이트(Arctic-ko 8081 필요)
MV3_RUN_EVAL_GATE=1 pytest tests/test_eval_gate.py -q -m slow
python -m src.eval_gate --qrels evals/recall_qrels.json --baseline evals/recall_baseline.json --k 5 --json

# A/B (Gemma 8080 + Arctic-ko 8081)
MV3_CR_MODE=synopsis python -m src.cr_backfill_cli --cr-backfill --mode synopsis
MV3_CR_SEARCH=1 python -m src.eval_gate --qrels evals/recall_qrels.json --baseline evals/recall_baseline.json
```

## 5. 합격 정의(Done)
- 816 + 신규 테스트 green, T-A1(off 회귀 0)·T-B2/T-B6(게이트 양방향) 통과.
- T-D1 코드참조 0 오류, T-D2 적대검증 2연속 결함 0 수렴.
- T-A6 A/B 결과가 수치로 status.md에 기록(채택/유지 결정 포함).

## 6. 구현 결과 (실측, 적대검증 수렴 후)

전체: **920 passed, 2 skipped**(816 기존 + ~104 신규 = 기능 테스트 + 적대검증 26 회귀 테스트, 41 subtests). 회귀 0.

| 파일 | 매핑 | 상태 |
|---|---|---|
| `tests/test_ranking_metrics.py` | T-B1 | ✅ |
| `tests/test_eval_runner.py` | T-B1b·T-B3·T-B4 + expected_top1 membership | ✅ |
| `tests/test_eval_gate.py` | T-B2·T-B5·T-B6 + 동적k·mrr fail-closed·use_ctx 가드·FP경계·비-dict baseline | ✅ (integration 1건 `MV3_RUN_EVAL_GATE=1` 시 통과) |
| `tests/test_migration_v4.py` | T-A1 + 동시성(race-swallow·8thread) | ✅ |
| `tests/test_cr_indexer.py` | T-A2 + degrade 내용·sanitize 변형 | ✅ |
| `tests/test_cr_search.py` | T-A3 + 손상ctx 폴백·강화 use_ctx | ✅ |
| `tests/test_cr_backfill.py` | T-A4 + off-refresh·embed-failure·인덱서 가드·락 직렬화 | ✅ |
| (기존) `test_schema_v2.py`·`test_memory_recall_deprecated.py` | 회귀 갱신 | ✅ SCHEMA_VERSION 4·`_vec_top_k` use_ctx 시그니처 |

- T-A1 off 회귀 0: ✅ (마이그레이션 nullable·column-가드 멱등, raw embedding 바이트 동일, off-mode 게이트 PASS).
- T-B2/T-B6 게이트 양방향: ✅ (evaluate pass/fail/regression/improve/fail-closed + CLI exit 0/1/2 end-to-end 실측).
- T-A6 A/B: ✅ **title·synopsis 양 티어 완전 동률**(off=CR-on, Δ=0) → A4 = CR 검색개선 0, 기본 off 유지(status.md §A/B).
- T-D1 doc-lint: ✅ (참조 파일·식별자 0 MISSING). T-D2 적대검증: ✅ **2연속 결함 0 수렴**(R9+R10, status.md 라운드 로그) + codex 2-track 교차검증.
