# Phase 1 ② — 효과적 회수 강화 (under-integration 해소) 설계 결정

- **작성일**: 2026-05-31
- **상태**: Draft (사용자 검토 대기)
- **상위 문서(단일 진실원천)**: `docs/specs/2026-05-30-second-brain-roadmap-design.md` §4.2②, §4.3, §4.4
- **선행 작업**: Phase 1 ①Provenance (v3.6.0, 출시·머지·배포 완료) — source 라벨이 회수 출력에 이미 부착됨
- **기준 버전**: master `a264865` (post v3.6.0)

> 이 문서는 로드맵 spec §4.2②가 남긴 설계 결정(계약 문구 위치·강도·목표 수치·위반 옵션 처리)을 확정하는 **focused 결정 기록**이다. 비전·로드맵의 단일 진실원천은 상위 문서이고, 본 문서는 그 §4.2②를 구현 가능한 형태로 좁힌다.

---

## 0. 한 줄 요약

회수된 메모리가 답변 reasoning 에 거의 통합되지 않는 **under-integration (strict cited 7.62% baseline)** 을 해소하기 위해, NEXT-37 의 약한 "회수 노트:" 한 줄에 **self-check 계약**(옵션·권장·다음 단계 제시 시 회수된 feedback·project 메모리의 명시 룰과 cross-reference)을 추가하고, strict cited 목표치를 **15%** 로 확정해 측정 게이트를 운영화한다.

---

## 1. 문제 (실측 근거)

- `src/self_eval.py` `recall_utilization`/`classify_recall_utilization` 측정: **strict cited = 7.62% baseline** (NEXT-37). 회수는 hard-gate(raw cosine + intent classifier)를 통과해 주입되지만, 답변 reasoning 에 실제 통합(cited)되는 비율이 극히 낮다.
- 뿌리: [[recalled-memory-weight]] — 같은 세션에서 형이 2회 명시 지적. 회수된 사실(`project_mindvault` = public·영상 ship 완료, `feedback_no_future_release_predictions` = 미래 release 예고 금지)을 메인 Claude 가 weight 0 처럼 다뤄, 잘못된 전제로 옵션을 제시 → 사용자 신뢰 손상.
- ①Provenance 가 토대를 깔았다: 회수 출력에 `출처:` 라벨이 부착돼 (a) 반영 동기 + (b) 검증 경로가 생겼다. ②는 그 위에서 회수를 답변에 **실제 통합시키는 계약**을 강화한다.

---

## 2. 설계 결정 (D1~D7)

### D1. self-check 계약의 위치 = **회수 hook 출력 (양 포맷터)**

`src/recall_core.py`의 `CONTRACT` 상수와 `hooks/memory-recall.py`의 `_format_output` 인라인 계약 문자열에 추가한다 (둘은 byte-parity).

- **근거**: spec §4.2② 명시 — "현재 '회수 노트:' 한 줄(NEXT-37)은 약한 강제. **여기에** self-check 계약을 추가." hook 출력은 (a) 실제 회수가 fire될 때만 주입 → 이미 raw cosine + intent classifier 로 hard-gated(picked>0 ≈ 66%, 잡담 차단) → **조건부**, (b) `tests/test_recall_core_parity.py`가 silent drift 차단, (c) 회수된 메모리 + `출처:` 라벨 바로 아래 배치돼 cross-reference 대상이 명확.
- **대안 (기각)**: `~/.claude/CLAUDE.md`의 "회수 알림 규칙" 섹션에 추가. always-on 이라 회수 없는 잡담 턴에도 토큰 소모, 회수 결과와 물리적으로 분리돼 "어느 메모리의 어느 룰" cross-reference 대상이 불명확. 또한 사용자 글로벌 파일 수정은 배포 성격(형 승인 경계).
- **보조 옵션 (형 승인 후 선택)**: CLAUDE.md "회수 알림 규칙"에 self-check 요약 1줄을 추가하면 hook 출력 계약을 상시 강화. 이번 범위에서는 **하지 않음** — hook 출력 단일 locus 로 시작하고, dogfood 후 효과 부족 시 형 승인하에 보강. (최종 보고 플래그)

### D2. 계약 강도 = **무조건 주입 + 문구로 type-scope** (조건부 렌더링 아님)

회수 결과가 있으면 self-check 줄을 항상 렌더한다. 단 문구 자체가 "feedback·project 메모리의 명시 룰"로 scope 한다.

- **근거**: TOP_K=1 이라 보통 1건 회수. 계약은 1문장(~80자)이라 토큰 영향 미미(§ "v1 토큰낭비 금지" 준수 범위). 문구가 스스로 scope 하므로 회수된 게 reference/user 타입이거나 명시 룰이 없으면 자연 no-op. 회수 결과 dict shape 는 `{path,name,description,snippet,score,raw_cosine,source,provenance}` 로 **`type` 필드가 없다** → 조건부 렌더링은 hot-path 결과에 type 부착 + 양 포맷터 분기 + parity 테스트 복잡화를 부르는데, 절감 이득(1줄 토큰)이 그 위험보다 작다.
- **대안 (기각)**: 회수된 메모리 `type ∈ {feedback, project}`일 때만 self-check 줄 렌더. 토큰 최소화는 되나 결과 shape 확장·양 포맷터 분기·parity 위험 증가로 ROI 낮음.

### D3. 계약 문구 (확정)

기존 `CONTRACT` 끝에 1문장 append:

> 옵션·권장·다음 단계 제시 시 위 feedback·project 메모리의 명시 룰과 충돌하는 항목은 제거하거나 "회수 메모리 X 위반 가능성"으로 표기.

전체 `CONTRACT` (변경 후):

```
답변 시작 전 한 줄로 "회수 노트: <위 메모리가 본 질문과 어떻게 관련되는가, 무관하면 '무관'>" 명시 출력 의무. 회수 fact 와 답변이 모순되면 즉시 표기. 옵션·권장·다음 단계 제시 시 위 feedback·project 메모리의 명시 룰과 충돌하는 항목은 제거하거나 "회수 메모리 X 위반 가능성"으로 표기.
```

- **근거**: [[recalled-memory-weight]]의 How-to-apply 3줄("옵션 제시/권장 path/다음 단계 제안 직전 cross-reference" + "위반 옵션 제거 또는 명시 표기")을 1문장으로 **계약 승격**. ①의 `출처:` 라벨이 바로 위에 있어 "신뢰 근거" 동반(spec §4.2② "①의 source 필드를 신뢰 근거로 활용 가능").
- **불변식**: 기존 "회수 노트:" 문구와 "모순되면 즉시 표기" 는 그대로 둔다 (self_eval `RECALL_MARKER_RE`, `RECALL_INJECTION_HEADERS`, `RECALLED_NAME_RE` 회귀 흉터 보호).

### D4. strict cited 목표치 = **15%** (≈2× baseline 7.62%)

완료 게이트 측정 조건:

- **1차 지표**: `utilization_rate_strict ≥ 0.15`
- **표본 게이트**: `judged ≥ 30` (cited + marker_only + unused; no_response 제외) — noise 방지
- **측정 윈도우**: 계약 배포 후 **≥ 1주 dogfood**
- **보조 지표(방향성)**: `unused` bucket 비중 감소, `lenient` rate 동반 상승

- **근거**: strict cited 는 substring-match 기반 **lower bound** (rephrase/의역 시 false negative). 따라서 100% 도달 불가 — spec §4.4 "100% 통합 약속 금지" 와 정합. 2× baseline 은 "검증 가능한 의미있는 상승"(spec §4.3②)이면서 단기 dogfood 로 현실적. judged ≥30 표본 게이트로 소표본 noise 차단.
- **★ 사용자 판단 플래그**: 정확한 수치(15% vs 12% vs 20%)는 형의 dogfood 위험선호(공격적 목표 vs 보수적)에 따라 조정 가능. plan 은 15% 로 확정하되 `--target` 인자로 런타임 조정 가능하게 설계 → 형이 재측정 시 값만 바꾸면 됨. (최종 보고 플래그)

### D5. 측정 게이트 운영화 = `recall_utilization_gate()` + `--target` CLI

`src/self_eval.py`에 추가:

- 상수 `RECALL_UTILIZATION_TARGET = 0.15`, `RECALL_UTILIZATION_MIN_JUDGED = 30`
- 함수 `recall_utilization_gate(util_result, target, min_judged) -> {pass, strict, target, judged, reason}`
- CLI `--target FLOAT` (default 상수): `--recall-utilization` 와 함께 주면 출력 dict 에 `gate` verdict 포함

- **근거**: spec §4.3② 완료 게이트("strict cited 상승")를 prose 가 아니라 **checkable pass/fail** 로 운영화. 기존 `recall_utilization` 측정 로직은 **불변**(철학 유지). 게이트는 측정값 위에 판정만 얹는다.

### D6. 자동 게이트 조정 미구현 유지

`self_eval` 은 측정·판정만 한다. 회수 raw cosine/score 임계값을 strict cited 결과로 **자동 튜닝하지 않는다**.

- **근거**: spec §4.4 — 잘못 학습된 loop 가 게이트를 망가뜨릴 위험이 자동화 이득보다 큼. false negative > false positive 정책 유지.

### D7. parity 불변식 유지 (제약)

- `recall_core.CONTRACT` ↔ `hooks/memory-recall.py` 인라인 계약 문자열을 **동시** 수정.
- `tests/test_recall_core_parity.py::test_formatter_byte_equivalence` (byte 동일성), `RECALLED_NAME_RE` 추출(`extract_recalled_ids_from_hook_injection`), `sanitize`(`</system-reminder>` 누출 차단), "회수 노트:" 계약 회귀 모두 통과 유지.
- compact 재주입(`src/session_memory.py`)은 `recall_core.format_memory_context` 경유라 `CONTRACT` 변경이 자동 전파 — 별도 수정 불필요(의도된 일관 전파).

---

## 3. 솔직한 한계 (spec §4.4 계승)

회수 통합은 결국 메인 Claude 의 행동이라 hook 이 100% 강제할 수 없다. 시스템 보장 범위 = **주입 + 출처(①) + 측정 + self-check 계약**까지. 본 작업은 "100% 통합"을 약속하지 않으며, 목표는 출처로 신뢰를 높여 자발적 사용 유도 + 측정으로 압박 + self-check 계약으로 옵션 게이팅을 유도하는 데까지다.

- self-check 계약은 **prompt-level 의무**일 뿐 코드 강제가 아니다. 모델이 무시하면 검출은 `self_eval` 사후 측정으로만 가능(실시간 차단 불가).
- strict cited 는 lower bound → 목표 미달이 곧 실패는 아님(의역 통합은 marker_only/unused 로 과소집계). 보조 지표(unused 감소)를 함께 본다.

---

## 4. 비범위

- ③ stale 자동 감지(코드/모델명/버전 참조 재검증, BGE→Arctic 회귀 케이스) — 별도 plan.
- 회수 임계값 자동 튜닝(D6) — 영구 미구현.
- CLAUDE.md "회수 알림 규칙" 보강(D1 보조 옵션) — 형 승인 후 선택.
- install.sh 재배포·GitHub push/tag/release — 형 승인 영역.

---

## 5. 완료 게이트 (검증 가능)

1. self-check 계약 문구가 양 포맷터에 추가되고 byte-parity 유지 (Task 1)
2. `recall_utilization_gate()` + `--target` CLI 로 strict cited 목표(15%) 대비 pass/fail 판정 가능 (Task 2)
3. 새 계약이 `self_eval` ingestion(`extract_recalled_ids_from_hook_injection`)·"회수 노트:"·sanitize 회귀를 깨지 않음 + 전체 회귀 통과 (Task 3)
4. 배포 후 ≥1주 dogfood 로 strict cited 정량 추적 (배포는 형 승인 영역 — 측정 절차만 plan 에 명시)

---

## 6. 출처 & 근거

- 상위 설계: `docs/specs/2026-05-30-second-brain-roadmap-design.md` §4.2②/§4.3/§4.4
- ① 선행: `docs/plans/2026-05-30-phase1-provenance.md` (출처 라벨 부착 — self-check 신뢰 근거)
- 결함 근거: [[recalled-memory-weight]] (How-to-apply 승격 원천), `src/self_eval.py` `recall_utilization` (strict cited 7.62%)
- parity 계약: `tests/test_recall_core_parity.py`, `src/recall_core.py`
