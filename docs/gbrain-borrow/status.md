# status.md — 진행 및 검증 로그

> 짝: [goal.md](./goal.md) · [plan.md](./plan.md) · [test.md](./test.md)
> 본 작업의 1차 산출물 = **검증된 4문서**. 코드 구현은 plan.md가 정의하는 후속.

## 현재 상태

| 항목 | 상태 |
|---|---|
| 워크트리 | `branch worktree-gbrain-borrow-docs` (HEAD a1a979e 기준) |
| goal.md | ✅ 초안 (코드 grounding 완료) |
| plan.md | ✅ 초안 (8 Phase, touchpoint file:line) |
| test.md | ✅ 초안 (매트릭스 + 적대검증 절차) |
| status.md | ✅ 본 문서 |
| 코드 구현 | ⬜ 미착수 (문서 수렴 후 별도) |
| 문서 수렴(D2) | ✅ 수렴 (라운드 1→2→3, 2연속 결함 0) |

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
