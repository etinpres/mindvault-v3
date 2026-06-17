# status.md — 진행 및 검증 로그

> 짝: [goal.md](./goal.md) · [plan.md](./plan.md) · [test.md](./test.md)
> 본 작업의 1차 산출물 = **검증된 4문서**. 코드 구현은 plan.md가 정의하는 후속.

## 현재 상태

| 항목 | 상태 |
|---|---|
| 워크트리 | `branch worktree-gbrain-borrow-docs` (docs commit 0eac53f 위) |
| goal.md / plan.md / test.md / status.md | ✅ 수렴 (라운드 1→2→3, 2연속 결함 0) |
| **코드 구현 Phase 1~6** | ✅ 완료 — 전체 pytest **898 passed, 2 skipped**(816 기존 + 82 신규, 회귀 0) |
| Phase 1 eval 코퍼스/스코어러/러너 | ✅ ranking_metrics·eval_runner·recall_qrels.json(32쿼리) |
| Phase 2 eval gate + baseline | ✅ eval_gate(dual-gate, exit 0/1/2)·recall_baseline.json·evals/README.md |
| Phase 3 스키마 v4 | ✅ indexer.py(_migrate_schema column-가드 멱등)·test_migration_v4 |
| Phase 4 인덱서 CR 생성 | ✅ memory_indexer.py(title/synopsis/Gemma 폴백·강등·off 폴백) |
| Phase 5 검색 CR 경로 | ✅ memory_search.py(_vec_top_k use_ctx·COALESCE 폴백·clean 쿼리) |
| Phase 6 CR 백필 CLI | ✅ cr_backfill_cli.py(stale 재임베딩·멱등·재개·dry-run) |
| Phase 7 A/B (title) | ✅ **완전 동률**(off=title-on, 전 메트릭·전 쿼리 rank 불변) |
| Phase 7 A/B (synopsis) | ✅ **완전 동률**(off=synopsis-on, Δ=0, 전 쿼리 rank 불변) — 261 Gemma 백필 후 측정 |
| A4 최종 판정 | ✅ **CR 검색 개선 0 → 기본 off 유지**(사유 기록). 게이트=회귀 보호 |
| 구현 적대검증 수렴 | 🔄 라운드 1(확정9)→2(확정10) 수정 완료, 라운드 3 진행(2연속 0 목표) |

## 구현 A/B 핵심 발견 (Phase 7, goal A4)

> **off-mode 가 라벨 코퍼스를 포화**(prod 261메모리, 32쿼리): recall@5=**1.000**,
> first_hit=**0.969**, mrr=**0.984**, expected_top1=**0.969**. 유일 비-top1 = q25
> lazy-timezone(off rank2, 근접 개념 메모리 module-env-lookup-pitfall 에 밀림).
>
> **원인(중요)**: MV 는 사람이 쓴 frontmatter `description` 을 **별도 1.5x 가중 벡터로
> 임베딩**한다 → 이미 강한 맥락 신호 보유. 즉 MV 는 원시적 CR 을 이미 내장한 셈.
> gbrain 은 raw chunk 만 임베딩(별도 synopsis 벡터 없음)이라 CR 한계효용이 컸지만,
> **MV 는 CR title-tier(name+description 선붙임)가 description 벡터와 중복** → 한계효용 낮음.
>
> **title-tier A/B = 완전 동률** (실측, prod title-백필 후 off vs CR-on): 모든 메트릭
> Δ=0, 32쿼리 중 rank 변동 0. title-tier CR 은 검색을 1ms·1순위도 안 바꿈(=description
> 벡터와 정보 중복 확정).
>
> **synopsis-tier A/B = 완전 동률** (실측, tmp 복사본 261 Gemma 백필 후 off vs CR-on,
> 동일 DB): 모든 메트릭 Δ=0, 32쿼리 rank 변동 0. **Gemma 가 description 과 다른 새 맥락을
> 생성해도** 검색 결과 불변 — description 별도 벡터가 이미 충분한 맥락 신호를 제공해
> 추가 contextual 임베딩의 한계효용이 0. q25 lazy-timezone(off rank2)도 양 티어에서
> 안 움직임(근접개념 메모리에 밀린 것이라 CR 이 못 고침).
>
> **A4 최종 판정**: off 포화 + title·synopsis 양 티어 동률 → **CR 의 검색 개선 = 0**
> (이 코퍼스·규모·MV 아키텍처에서). 따라서 **CR 기본 비활성(off) 유지** — A4 의 "동률/악화
> 시 기본 off + 사유 기록" 분기 충족. eval gate 의 1차 가치는 **회귀 보호**(off baseline
> 고정 → CR/미래 검색변경의 정확도 하락 자동 차단). CR 기계 자체는 완전 구현·테스트되어
> opt-in(MV3_CR_MODE 백필 + MV3_CR_SEARCH)으로 언제든 켤 수 있으나, gbrain(raw chunk만
> 임베딩)과 달리 MV 는 description 벡터로 이미 CR 효과를 내장해 채택 이득이 없다.
> → plan.md Q1(title vs synopsis) 답: **둘 다 불필요**(synopsis Gemma 비용 정당화 안 됨).
> → plan.md Q3(운영 hook CR 기본전환) 답: **전환 안 함**(A4 미통과 = 개선 0).

## 재개 가이드 (compact/새 세션 핸드오프)

> 이 대화가 compact되거나 새 세션에서 시작해도 **이 4문서만으로 재개 가능**. 진입 순서:
> 1. **이 status.md** 먼저 — 현 상태·수렴 여부·다음 액션.
> 2. `goal.md` — 목표·제약·성공기준(§2에 MV 코드 실측 file:line 박혀 있어 **재탐색 불필요**).
> 3. `plan.md` — **Phase 1부터** 구현(eval gate: `ranking_metrics`→`recall_qrels.json`→`eval_runner`→`eval_gate`). gbrain CR/eval 포팅 알고리즘이 이미 증류돼 있음.
> 4. `test.md` — 기준↔케이스 매트릭스로 TDD.
>
> **위치**: 워크트리 `.claude/worktrees/gbrain-borrow-docs`, 브랜치 `worktree-gbrain-borrow-docs`, 문서 `docs/gbrain-borrow/`.
> **현재**: 문서 수렴 완료(코드 미착수). 다음 = plan.md Phase 1.
> **내구성**: 4문서=디스크 영구. ⚠️ **휘발**: gbrain 클론이 job-tmp(`~/.claude/jobs/*/tmp/gbrain`)라 job 삭제 시 증발 — 단 알고리즘은 plan.md에 증류됨, 재접근 필요 시 `github.com/garrytan/gbrain` 재클론. 4개 매핑 에이전트 원문은 세션 transcript에만(핵심은 goal §2/plan에 증류).
> **미커밋**: 문서는 워크트리에 있으나 git 커밋 안 됨(사용자 미요청). 영구 보존하려면 커밋/머지 필요.

## 코드 grounding 근거 (검증 출처)

직접 grep 검증한 load-bearing 사실 (status 작성 시점 worktree):
- `SCHEMA_VERSION = 3` @ `src/indexer.py:53`; `_migrate_schema` @ `:274`.
- `memories_vec(path, kind, embedding BLOB)` @ `src/indexer.py:247-253`.
- **sqlite-vec 미사용** — 일반 BLOB+numpy @ `src/indexer.py:210-211`, `src/memory_indexer.py:5-6` (macOS Python `enable_load_extension` 미지원). ⚠️ CLAUDE.md "sqlite-vec" 표기 부정확 — 코드 기준 채택.
- `recall_memory` @ `src/memory_search.py:550`; 상수 `RRF_K=60 DESCRIPTION_WEIGHT=1.5 DEFAULT_TOP_K=1 DEFAULT_THRESHOLD=0.50 DEFAULT_RAW_COSINE_MIN=0.32 DEPRECATED_DECAY=0.3` @ `:40-57`.
- `incremental_index` @ `src/memory_indexer.py:379`; `full_rebuild` @ `:510`; CR 통합점 `:443-498`.
- `self_eval.py` 운영지표만(labeled corpus·P@k·회귀게이트·CI 없음); `recall_utilization_gate` @ `:1082-1122`.
- `metrics.jsonl` 필드에 `recalled_ids` 존재(코퍼스 시드 후보).
- pytest **816** 수집(`pytest --collect-only`), `.github/workflows` 없음(로컬 전용).
- 4개 매핑 에이전트 리포트(MV 임베딩 / MV eval / gbrain CR / gbrain eval) — 원문은 세션 기록.

## 미해결/결정 대기 (plan.md Q1~Q3)
- Q1. title(무료) vs synopsis(Gemma) — Phase 7 A/B 데이터로 결정.
- Q2. 장문 절단(8192토큰)은 CR 비해결 → 청킹 후속(비범위).
- Q3. 운영 hook 검색 CR 기본전환은 A4 통과 후 **사용자 결정**(자동 금지).

---

## 검증 라운드 로그 (오류 0 수렴까지)

> 절차: test.md §3. 라운드마다 4 리뷰어(정확성/실현가능성/제약/일관성)가 코드 대조 → `[리뷰어] 결함 → 조치`. **2연속 결함 0** = 수렴.

### 라운드 0 — 초안 작성 (자기검증)
- [정확성] 핵심 file:line 직접 grep 검증 완료(위 근거 표). sqlite-vec 표기 오류 발견 → 문서에 코드기준 교정 반영. 결함 0(자기검증 한계 있음 → 라운드 1에서 독립 검증).
- 상태: 독립 적대 검증(라운드 1) 대기.

### 라운드 1 — 독립 적대 검증 (워크플로 wf_1f1c0116)
- 4 리뷰어(정확성/실현가능성/제약/일관성) + 판정자(코드 재검증). 후보 13건 → **확정 6건**(blocker 0, major 4, minor 2).
- 확정 결함 → 조치:
  1. [정확성·major] pytest 개수 **814 → 816**(실측 `pytest --collect-only`=816; `def test_` grep 814는 클래스/파라미터화 2건 누락). goal/plan/status/test 10곳 전부 교정 + 출처 주석. → 수정 완료.
  2. [일관성·major] test.md **T-B1이 B2 미완 커버**(first_relevant_hit·expected_top1·JSON리포트/baseline 생성 미검증). → T-B1에 두 메트릭 assert 추가 + **T-B1b**(러너 리포트/baseline 5키 생성, mock recall) 신설 + 매트릭스 행 추가. 수정 완료.
  3. [일관성·minor] **`MV3_CR_SEARCH`** 값 표기 불일치(`=off` vs `=1`). → 숫자 컨벤션 `=1`/`=0`로 통일(test.md:83). 수정 완료.
  4. [실현가능성·minor] plan.md §2.3 **skip 스코프 모순**(파일 전체 skip이 순수 evaluate() 테스트까지 skip). → hermetic(항상 실행)/integration(skip) **두 그룹 분리**로 재작성. 수정 완료.
- false positive 7건은 판정자가 코드 재검증으로 기각(라인 드리프트·중복 등).
- **결함 0 아님 → 라운드 2 필요**(수렴 미달).

### 라운드 2 — 수정 검증 + 신규 결함 (워크플로 wf_26070e24)
- 라운드 1 수정본 재검증. 후보 5건 → **확정 0건**(blocker 0, major 0, minor 0).
- 라운드 1 수정이 신규 결함 유발 없음. 후보 5건은 판정자가 코드 재검증으로 전부 기각(false positive).
- **결함 0 (1회차)** — "2연속 0" 기준 위해 라운드 3 1회 더.

### 라운드 3 — 안정성 확인 (워크플로 wf_47a15abb)
- 재검증. 후보 1건 → **확정 0건**. 판정자 코드 재검증으로 기각.
- **결함 0 (2회차 연속)**.

### ✅ 수렴 선언
- 라운드 1(확정 6 → 수정) → 라운드 2(확정 0) → 라운드 3(확정 0). **2연속 결함 0 → 수렴**(test.md §3 기준 충족).
- 최종 sanity: 잔여 `814` = 의도된 설명 주석 2곳뿐(나머지 pytest 참조 전부 816), 4문서 존재(610줄), 신규 파일명·신컬럼명 plan↔test 교차 일관.
- D1(코드참조 0 오류)·D2(적대검증 0 수렴) 충족 → **본 작업(검증된 4문서) 종료조건 달성**.
- 잔여(후속 작업): plan.md Q1~Q3 의사결정은 코드 구현 Phase 4·7에서 데이터로 닫음. 코드 구현 자체는 본 산출물 범위 밖.

---

## 구현 적대검증 라운드 로그 (코드 — 오류 0 수렴까지)

> 절차: test.md §3 을 *구현 코드* 에 적용. 라운드마다 4차원 finder(cr-correctness/
> eval-correctness/constraint/test-adequacy) → 판정자가 코드 대조로 false positive 기각
> → 확정 결함만 수정 → 재검증. **2연속 확정 0** = 수렴.

### 라운드 1 — 독립 적대 검증 (워크플로 wf_71804d30, 15 agents)
- 후보 11 → **확정 9 / 기각 2**(blocker 1, major 4+1test, minor 3).
- 확정 결함 → 수정:
  1. **[blocker] eval_gate `--k≠5`**: `evaluate` 가 `mean_recall_at_5` 하드코딩 vs `score_corpus` 동적 키 `mean_recall_at_{k}` → k≠5 시 fail-closed 오판 + 회귀게이트 무력화. → `evaluate(...,k)` 동적 키 + main 전달 + baseline k 불일치 EXIT_USAGE 가드. **수정**.
  2. **[major] 동 근본(비-json 출력)**: `--k≠5` 비-json 에서 `m['mean_recall_at_5']` KeyError 크래시. → 동적 출력 키 `.get(...,0.0)`. **수정**.
  3. **[major] 동 근본(baseline 영속)**: `--k≠5 --update-baseline` → baseline 에 다른 k 키 → 회귀게이트 영구 무력화. → k-가드로 차단. **수정**.
  4. **[minor] mrr fail-closed 비대칭**: current 에 mrr 키 없으면 조용히 pass. → mr/fh 와 대칭으로 None→fail_closed. **수정**.
  5. **[major] `_vec_top_k` COALESCE**: 손상(non-NULL) embedding_ctx 면 행 통째 skip, raw 폴백 안 됨 → 영구 미회수. → COALESCE 제거, 두 컬럼 SELECT + `_decode_vec` 로 ctx 우선·실패 시 raw 폴백. **수정**.
  6. **[major] off-mode 재인덱싱이 백필 ctx 파괴**: 파일 편집 시 off-mode DELETE+재INSERT 가 embedding_ctx→NULL, corpus_generation→off 로 백필 파괴·비수렴. → off 모드는 기존 CR 컬럼 carry-forward(파괴 금지). **수정**.
  7. **[minor] `_sanitize_ctx` 대소문자/공백 변형 미제거**. → `re.IGNORECASE` 정규식 `</?\s*context\s*>`. **수정**.
  8. **[major-test] `test_recall_use_ctx_false_ignores_ctx` 거의 vacuous**: off↔on 비교·raw_cosine 검증 없음. → off raw_cosine==raw embedding 직접코사인 + off↔on 차이 assert 강화. **수정**.
  9. **[minor-test] degrade 테스트 내용 미검증**: cr_synopsis==description 미확인. → 내용 동등성 assert 추가. **수정**.
- false positive 2건 판정자 기각. 회귀 테스트 9개 신설(동적k·mrr·손상ctx폴백·off보존·sanitize변형 등).
- 결과: 전체 **906 passed, 2 skipped**. off-mode 게이트 PASS(parity 유지). **확정>0 → 라운드 2 필요**.

### 라운드 2 — 수정 검증 + 신규 결함 (워크플로 wf_b086cecc, 15 agents)
- 후보 11 → **확정 10 / 기각 1**(major 4, minor 6). 라운드1 수정의 *더 깊은* 후속 결함 포함(적대검증 정상 작동).
- 확정 결함 → 수정:
  1. **[major] cr_backfill 임베딩 실패 영구 sentinel**: 백필 중 임베딩서버 다운 시 `compute_contextual_embedding`→(None,None,"off") 인데 corpus_generation=target_gen 마킹 → 영구 제외(indexer.py EmbedUnavailable 가드 미적용). → `effective=="off" and body.strip()` 시 마커 미기록·`failed_embed` 카운트·재시도. **수정**.
  2. **[major] off-preserve(R1 수정)가 corpus_generation 까지 보존 → stale ctx 영구 미갱신**: 내 R1 fix 가 과보존. → embedding_ctx *벡터* 만 보존(파괴 0), cr_mode/corpus_generation 은 off 기본 → 다음 백필이 refresh. **수정**.
  3. **[minor] title+빈 description = name-only 인데 cr_mode="title"**: 라벨 nuance(embedding_ctx 는 정확히 채워짐). name 도 유효 title 맥락이라 동작 정상 — 주석 명시로 수용. (fixtures 미해당)
  4. **[minor] off + body 빈편집 시 carry-forward 불일치**: R2-#2 수정(cr_mode/gen 미보존 + body INSERT 는 vec_body 가드)으로 동반 해소. **수정**.
  5. **[major] `_load_baseline` 비-dict 루트 AttributeError 미캐치 크래시**. → isinstance(dict) 가드 → ValueError → EXIT_USAGE. **수정**.
  6. **[major] threshold 키 정적(`min_recall_at_5`) vs recall 키 동적**: --k≠5 시 recall@k 를 min_recall_at_5 로 게이트. → `min_recall_at_{k}` 폴백 `min_recall_at_5`. **수정**.
  7. **[minor] load_qrels expected_top1 ∉ relevant 미검증**. → membership 체크. **수정**.
  8. **[minor] `_write_baseline` slim 키 `recall_at_5` 하드코딩**. → `recall_at_{k}`. **수정**.
  9. **[minor] run_corpus use_ctx 미고정(call-time env)**: 결정성 주장 약화. → 런 시작 1회 고정 + 리포트 기록. **수정**.
  10. **[minor-test] test_cli_k10 출력값 미검증**. → capsys 로 `recall@10` 실제값 assert. **수정**.
- 회귀 테스트 6개 신설(임베딩실패 sentinel·off refresh·비-dict baseline·expected_top1 membership·k10 출력 등).
- 결과: 전체 **910 passed, 2 skipped**. off-mode 게이트 PASS. **확정>0 → 라운드 3 필요**.

### 라운드 3 — 안정성 확인 (워크플로 wf_21e58c4b, 6 agents)
- 후보 2 → **확정 1 / 기각 1**(minor 1). 9→10→**1** 강한 수렴.
- 확정 결함 → 수정:
  1. **[minor] v3→v4 마이그레이션 TOCTOU race**: `_add_column_if_missing` check-then-ALTER 가 무락 open_db 동시호출(recall 경로 락 없음, WAL 동시오픈 전제)에서 일회성 전환 윈도우에 두 프로세스가 동시 ALTER → loser "duplicate column" 미가드(self-healing 이나 정당). → ALTER 를 try/except OperationalError 로 benign duplicate swallow(다른 오류 전파). 동시성 회귀 테스트 2개 추가(8-thread open_db + race 모사). **수정**.
- 결과: 전체 **912 passed, 2 skipped**. **확정>0(1) → 라운드 4 필요**(2연속 0 미달).

### 라운드 4 — 수렴 확인 (워크플로 wf_28d9312f, 4 agents)
- 3차원(cr-impl/eval-impl/integration-and-test) 엄격 스윕. 후보 1 → **확정 0 / 기각 1**.
- 라운드3 수정(race-swallow) 무결성 확인, 신규 결함 0. **결함 0 (1회차)** — "2연속 0" 위해 라운드 5.

### 라운드 5 — 독립 최종 스윕 (워크플로 wf_30baa653, 6 agents)
- 3차원(data-flow/edge-cases/constraint) 스윕. 후보 3 → **확정 3 / 기각 0**(major 1, minor 2). 전부 이전 라운드 부분수정의 *미완성 지점*("한 곳 고치고 다른 곳 누락").
- 확정 결함 → 수정:
  1. **[major] 인덱서 CR 경로 embed-failure 영구마커**: R2-1 을 cr_backfill 엔 넣었으나 incremental_index CR 경로엔 누락. MV3_CR_MODE=title/synopsis 인덱싱 중 ctx 임베딩 실패 시 corpus_generation=gen(설정모드) 마킹 → 영구 제외. → `cr_mode_active!="off" and effective=="off" and body.strip()` 시 gen("off")(가드 동형). **수정**.
  2. **[minor] eval_gate use_ctx 미기록·미가드**: R2-9 가 report 에 use_ctx 만 넣고 baseline 영속·비교 가드 누락 → CR-on 평가가 CR-off baseline 과 비교될 수 있음. → baseline 에 use_ctx 기록 + main 에 k-가드 동형 use_ctx 불일치 EXIT_USAGE. **수정**.
  3. **[minor] `_load_baseline` sub-field 미검증**: R2-5 가 루트만 검증, thresholds/metrics 비-dict 면 `.get()` AttributeError 크래시. → sub-field isinstance 검증 → ValueError. **수정**.
- 회귀 테스트 5개 신설. **선제 일관성 감사**: corpus_generation/embedding_ctx WRITE 전지점·use_ctx report 양 경로 가드 일관 확인.
- 결과: 전체 **917 passed, 2 skipped**. off baseline use_ctx=false 재고정·게이트 PASS. **확정>0(3) → 라운드 6 필요**(수렴 리셋).

### 라운드 6 — 수렴 재확인 (워크플로 wf_47e1c010, 4 agents)
- 3차원(guard-consistency/cr-impl/eval-impl) 스윕 + 선제 일관성 감사. 후보 1 → **확정 0 / 기각 1**.
- R5 수정(가드 전지점 적용·use_ctx·sub-field) 무결성 확인, 신규 결함 0. **결함 0 (1회차)** — "2연속 0" 위해 라운드 7.

### 라운드 7 — 독립 최종 스윕 (워크플로 wf_4bff1695, 6 agents)
- 3차원(fresh-cr/fresh-eval/fresh-tests, 사전지식 없는 fresh 리뷰어 시점) 스윕. 후보 3 → **확정 1 / 기각 2**(minor 1).
- 확정 결함 → 수정:
  1. **[minor] regression 비교 float 경계 오차**: `drop = b-m; if drop > max_drop` 가 정확히 허용폭인 drop(예 1.0-0.97=0.0300…027 > 0.03)을 가짜 regression fail. → `FP_EPS=1e-9` 톨러런스(`> max_drop + FP_EPS`), correctness floor(`< thr - FP_EPS`)도 일관 적용. **수정**. 회귀 테스트 2개.
- 결과: 전체 **918 passed, 2 skipped, 1 env-flake**(아래). **확정>0(1) → 라운드 8 필요**(수렴 리셋).

> **환경 flake 주석**: `test_e2e_4_hook_performance`(기존 816 중 1, 내 신규 아님)가 avg 360~431ms>150ms 로 실패. 원인 = **외부 job(`3ffba88a`)의 `arctic_loadtest.py`가 공유 Arctic MLX 서버(8081)를 saturate** → embed 2~5s(정상 <100ms, recall 5s timeout 히트). 실측 분리: Arctic embed 단독 4.4/3.2/5.0s. perf 제외 전체 **918 passed**. 내 off-mode recall 경로는 `_decode_vec` 도입 후에도 SELECT·검증 동일(구조적 지연 0). → **내 코드 회귀 아님**, Arctic 부하 회복 시 통과.

### 라운드 8 — 수렴 재확인 (워크플로 wf_9a574e11, 4 agents)
- 3차원(fp-and-numeric/cr-final/eval-final) 스윕. 후보 1 → **확정 1 / 기각 0**(major 1).
- 확정 결함 → 수정:
  1. **[major] cr_backfill 무락 ↔ 동시 hook reindex lost-update race**: incremental_index 는 memory-indexer.lock 을 잡으나 cr_backfill 은 무락 → 수동 백필 read(body v1)-compute(Gemma, 긴 윈도우)-write 중 off-mode 인덱서가 body v2 처리(DELETE+INSERT raw v2) 후 백필이 ctx(v1)+gen(title) write → raw(v2)/ctx(v1) 영구 불일치 + 가짜 converged(양 경로 영영 미수정). MV3_CR_SEARCH=1 시에만 발현하나 silent·non-self-healing. → cr_backfill 도 `_acquire_lock(db_path)` 로 인덱서와 직렬화(busy 시 abort). **수정**. 회귀 테스트(락 보유 중 abort→해제 후 처리).
- 결과: 전체 **920 passed, 2 skipped**(perf 테스트 Arctic 재기동 후 green). **확정>0(1) → 라운드 9 필요**.

### 라운드 9 — 수렴 확인 (워크플로 wf_493eb3fb, 3 agents)
- 3차원(concurrency/cr-final/eval-final) 스윕. **후보 0 / 확정 0** — finder 가 flag 할 결함조차 없음(가장 강한 dry 신호).
- R8 락 수정 무결성 확인, 신규 결함 0. **결함 0 (1회차)** — "2연속 0" 위해 라운드 10.

### 라운드 10 — 최종 수렴 확인 (워크플로 wf_54391fbb, 3 agents)
- 3차원(whole-cr/whole-eval/whole-test) 독립 정독. **후보 0 / 확정 0**.
- **결함 0 (2회차 연속, 라운드 9+10)** → Claude 적대검증 트랙 수렴.

### ✅ 코드 적대검증 수렴 선언 (Claude 트랙)
- 추세: 라운드 1(**9**)→2(**10**)→3(**1**)→4(0)→5(**3**)→6(0)→7(**1**)→8(**1**)→9(0)→10(0).
- 확정 결함 **누적 26건 전부 수정**(blocker 1·major 9·minor 14·test 2), false positive 다수 판정자 기각, 회귀 테스트 **26개 신설**.
- **2연속 결함 0(R9+R10)** → test.md §3 수렴 기준 충족. 전체 **920 passed, 2 skipped**.
- 방법론: [[systematic-debugging-code-review]] "0건 2회 수렴" 패턴 + [[project-mindvault]] "5라운드 적대 audit" 확립 패턴 일치. 단 단일트랙(Claude) → MV 정석인 codex 독립 2nd 트랙 교차검증 추가(아래).

### 라운드 11 — codex 독립 교차검증 (2-track)
- codex:codex-rescue 독립 정적 분석. **functional 3건 적발**(Claude 10라운드가 놓침 → 2-track 가치 입증). 코드 대조로 전부 검증·수정:
  1. **[minor] cr_backfill raw-stale skew**: 파일 편집 후 incremental_index 미실행 메모리에 백필이 현재 파일로 ctx 생성 → ctx(v2)/raw(v1) 혼합 + 가짜 converged(mtime 미확인). → 백필이 `mtime_ns` 불일치 메모리 skip(`skipped_stale`) — 인덱서가 먼저 raw 갱신. **수정**.
  2. **[minor] off-reindex stale ctx 랭킹 오염**: R1 off-preserve 가 body 변경 시에도 ctx carry-forward → use_ctx 검색이 stale ctx(v1) 로 랭킹·v2 row 반환(내 "raw라 무해"는 use_ctx=False 한정). → off-preserve 를 **body 불변 시에만** 보존(FTS 구body 비교), 변경 시 ctx 무효화(NULL)+gen(off)→백필 refresh. **수정**.
  3. **[minor] baseline metrics 비-숫자 값 TypeError 크래시**: 문자열 metric 값이 `b-m` 산술에서 미캐치 TypeError(exit 계약 위반). → `_load_baseline` 숫자 타입 검증→ValueError→EXIT_USAGE. **수정**.
- 회귀 테스트 4개 신설(raw-stale skip·body변경 ctx무효화·body불변 보존·비숫자 metric). 전체 **923 passed, 2 skipped**. **codex track 확정>0(3) → 양 트랙 재검증 필요**.

### 라운드 12 — 2-track 재검증
- **codex 재check**: R11 3건 closed·회귀 0 확인 + **NaN/Infinity baseline 우회 1건 추가**(json.loads 가 NaN/Inf 리터럴 파싱→isinstance float 통과→NaN 비교 전부 False 라 게이트 silent 우회). → `_load_baseline` 에 `math.isfinite` 검증. **수정**. 이후 codex 최종 재check: **functional 0건**(current-side NaN 은 score_corpus 가 0.0 처리라 무관 — 검증). codex 트랙 수렴.
- **Claude R12**: codex 수정 재검증 중 **major 1 적발** — synopsis 모드 Gemma 일시중단→title 강등(effective="title", Arctic 정상이라 ctx 는 생성)이 R2/R5 가드(effective=="off"만)를 빠져나가 gen("synopsis") converged 마킹 → Gemma 복구돼도 영구 title 고정. **근본원인**: corpus_generation 을 *설정모드* 로 마킹한 설계 결함. → **gen(effective_mode)** 로 통일(달성 tier 기준): synopsis 달성→gen(synopsis) 수렴, title 강등→gen(title)≠target→백필 재시도, off→gen(off)(R5 가드 subsume). 인덱서+백필 양 파일 수정, 회귀 테스트 2개(강등 후보 유지·복구 수렴). **수정**.
- 결과: 전체 **926 passed, 2 skipped**. 두 트랙이 *서로 다른* 결함 발견(codex=NaN, Claude=synopsis-degrade) — 2-track union 가치 입증. **양 트랙 재검증 필요**(corpus_generation 시맨틱 변경).

### 라운드 13 — 2-track 재검증
- **codex 재check**: synopsis-degrade fix·SELECT/UPDATE 비대칭 수렴·전반 → **functional 0건**.
- **Claude R13**: gen(effective) 수정의 *부작용* 1건(major+minor 동일 이슈) 적발 — **빈 body(frontmatter-only) 비수렴**: R12 가 corpus_generation=gen(effective) 로 바꾸면서, 빈-body 는 effective="off"→gen("off")≠gen(설정) 라 백필 후보 SELECT 가 매 run 재선정(무한 no-op, loop-until-dry 드라이버 hang 위험). R2 가드는 body.strip() True 만 차단해 빈-body 통과. → **빈-body 는 설정모드 기준 수렴 마킹**(`effective=="off" and not body.strip()` → target_gen) 양 파일 적용, R12 강등·R2/R5 embed-failure 불침해. 회귀 테스트(빈-body 2회차 candidates=0). **수정**.
- ⚠️ 운영 함정: R13 finder 서브에이전트가 워크트리 cr_backfill_cli.py 를 실험적으로 mutate(FIX-PROBE 잔여 후 revert) — [[workflow-worktree-cd-stale]] 패턴. 최종 검증(R14)은 **read-only 명시**로 재발 차단.
- 결과: 전체 **927 passed, 2 skipped**. 두 트랙 또 *서로 다른* 발견(codex=0, Claude=빈-body). **재검증 필요**.

### 라운드 14 — 2-track 재검증 (read-only)
- **codex**: 빈-body fix 닫힘·회귀 0·인덱서/백필 일관 → **functional 0건**.
- **Claude R14(read-only)**: 1건(minor) — **use_ctx=True 게이트 미스캘리**: `raw_cosine_map` 이 body 행에 ctx 코사인을 담는데 recall_memory 가 raw-캘리브 게이트(0.32)를 그대로 적용 → ctx 가 코사인 분포를 옮겨 미스게이팅. *experimental 경로 한정*(MV3_CR_SEARCH=1, prod 회수 use_ctx=False 라 무영향, A4 도 CR 미채택). → `_vec_top_k` 에서 body 행을 **ctx 로 랭킹·raw 로 게이트**(gate_mat 분리), off(use_ctx=False)는 gate_sims=sims 로 바이트 동일. 테스트 2개 새 동작으로 갱신. **수정**.
- ⚠️ read-only 명시로 서브에이전트 워크트리 mutation 재발 0.
- 결과: 전체 **927 passed, 2 skipped**. off-mode 게이트 PASS(parity). **재검증 필요**(_vec_top_k 변경).

### 라운드 15 — 2-track 최종 (read-only)
- **codex**: gate-by-raw(ctx 랭킹·raw 게이트) 확인, off 바이트동일, valid/meta/gate_mat 정합, 추가 결함 없음 → **functional 0건**.
- **Claude R15(read-only)**: 3차원 스윕 **후보 0 / 확정 0**(finder 가 flag 할 결함조차 없음).
- → **양 독립 트랙이 동일 최종 코드에서 functional 0 = 2-track 수렴.**

### ✅ 2-track 적대검증 수렴 선언 (코드)
- **Claude 트랙(15라운드)**: 9→10→1→0→3→0→1→1→0→0 / (codex-fix 재검증) 1→1→1(빈body)→1(gate)→0.
- **codex 트랙(5패스)**: 3→1(NaN)→0 / synopsis-degrade 확인 → 빈body 확인 → gate-by-raw 확인 → **0**.
- **확정 결함 누적 ~33건 전부 수정**(blocker 1·major ~11·minor ~21), false positive 다수 판정자/재검증 기각, **회귀 테스트 ~40개 신설**.
- 두 트랙이 *서로 다른* 결함 발견(codex=데이터skew·NaN·입력검증, Claude=synopsis-degrade·빈body·gate-by-raw) → [[feedback-codex-independent-verify]] "2-track union" 가치 실증. [[systematic-debugging-code-review]] "0건 (양트랙) 수렴" + [[haruko-novel-system-audit]] dry 수렴 패턴 일치.
- 운영 함정 기록: R13 finder 서브에이전트 워크트리 mutation([[workflow-worktree-cd-stale]]) → R14+ read-only 명시로 차단.
- **최종 상태**: 전체 **927 passed, 2 skipped**, off-mode 게이트 PASS(parity 바이트동일), A4 판정 확정(CR 검색개선 0 → 기본 off, 게이트=회귀보호).
- D1(코드참조 0)·D2(적대검증 수렴) 충족 + A1(off 회귀 0)·B2/B6(게이트 양방향)·A6(A/B 수치) 충족 → **구현+검증 종료조건 달성**.
