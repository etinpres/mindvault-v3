# Phase 1 ② — 효과적 회수 강화 (under-integration) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 회수된 메모리가 답변 reasoning 에 거의 통합 안 되는 under-integration(strict cited 7.62% baseline)을 해소하기 위해, 회수 hook 출력 계약에 self-check 조항(옵션·권장·다음 단계 제시 시 회수 메모리에 명시된 룰·제약과 cross-reference — audit R2E-1 후 content+이름 기반)을 추가하고, strict cited 목표(15%)를 checkable 게이트로 운영화한다.

**Architecture:** 두 포맷터(`src/recall_core.py`의 `CONTRACT` ↔ `hooks/memory-recall.py`의 `_format_output` 인라인 문자열)에 self-check 조항을 byte-동일하게 추가한다(둘은 `tests/test_recall_core_parity.py`가 byte-parity 강제). 측정은 기존 `src/self_eval.py` `recall_utilization`(strict cited 측정 인프라)를 불변으로 두고, 그 결과 위에 `recall_utilization_gate()` 판정 함수 + `--target` CLI 플래그만 얹는다. 새 계약 문구가 `self_eval` ingestion(`extract_recalled_ids_from_hook_injection`)·sanitize·"회수 노트:" 회귀를 깨지 않음을 e2e 로 고정한다.

**Tech Stack:** Python, pytest/unittest, 기존 hook 시스템. 제약: CC 내부 전용 / 운영비 0(로컬 MLX) / v1 토큰낭비 금지(계약 문구 1문장 ~80자로 한정) / 두 포맷터 byte-parity 불변 / hot-path(`memory-recall.py`) 회귀 흉터 보호.

**설계 단일 진실원천:** `docs/specs/2026-05-31-phase1-effective-recall-design.md` (D1~D7), 상위 `docs/specs/2026-05-30-second-brain-roadmap-design.md` §4.2②/§4.3/§4.4.

**Scope:** Phase 1 ②(효과적 회수)만. ①provenance(v3.6.0 완료)·③stale 감지는 별도. 자동 게이트 조정은 미구현 유지(D6). install.sh 재배포·GitHub push 는 형 승인 영역(비범위).

---

## File Structure

- Modify: `src/recall_core.py` (`CONTRACT` 상수 — self-check 조항 append)
- Modify: `hooks/memory-recall.py` (`_format_output` 인라인 계약 문자열 — 동일 조항 append, byte-parity)
- Modify: `src/self_eval.py` (`RECALL_UTILIZATION_TARGET`/`RECALL_UTILIZATION_MIN_JUDGED` 상수 + `recall_utilization_gate()` 함수 + `--target` CLI 플래그)
- Test: `tests/test_recall_core_parity.py` (self-check 조항 존재 + byte-parity + ingestion 회귀)
- Test: `tests/test_self_eval.py` (게이트 함수 pass/fail/insufficient-sample)

각 Task 는 독립 testable. TDD: 실패 테스트 → 확인 → 구현 → 통과 → 회귀 → 커밋.

**테스트 import 관례(확인됨):** `tests/conftest.py`가 `src/`를 `sys.path`에 추가 + env 격리. 따라서 `import recall_core`, `from self_eval import ...` 직접 import 가능. 하이픈 파일 `hooks/memory-recall.py`는 `tests/test_recall_core_parity.py`의 기존 `_load_memrecall()` 헬퍼로 로드.

---

### Task 1: self-check 계약 조항을 양 포맷터에 추가 (byte-parity 유지)

**Files:**
- Modify: `src/recall_core.py:32-36` (`CONTRACT`)
- Modify: `hooks/memory-recall.py:347-351` (`_format_output` 끝 `lines.append(...)`)
- Test: `tests/test_recall_core_parity.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_recall_core_parity.py` 맨 끝에 추가 (기존 `_load_memrecall()` 헬퍼 재사용):

```python
def test_self_check_clause_present_and_parity():
    """②효과적 회수 — self-check 계약(옵션·권장·다음 단계 직전 cross-reference)이
    양 포맷터 출력에 존재하고, 기존 "회수 노트:" 계약과 byte-parity 모두 유지."""
    import recall_core
    mr = _load_memrecall()
    sample = [{"name": "m", "source": ["vec"], "description": "d",
               "snippet": "", "score": 0.6}]
    out_core = recall_core.format_memory_context(sample, wrap_system_reminder=True)
    out_mr = mr._format_output(sample)
    # self-check 조항 핵심 토큰 (D3 확정 문구)
    assert "옵션·권장·다음 단계" in out_core
    assert "위반 가능성" in out_core
    assert "feedback·project" in out_core
    # 기존 NEXT-37 계약 불변 (회귀 흉터 보호)
    assert "회수 노트:" in out_core
    assert "모순되면 즉시 표기" in out_core
    # D7 byte-parity (한쪽만 바뀌면 실패)
    assert out_core == out_mr
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python3 -m pytest tests/test_recall_core_parity.py::test_self_check_clause_present_and_parity -v`
Expected: FAIL (`assert "옵션·권장·다음 단계" in out_core` — 아직 미추가)

- [ ] **Step 3: `recall_core.CONTRACT` 에 조항 추가**

`src/recall_core.py`의 `CONTRACT` (현재):

```python
CONTRACT = (
    "답변 시작 전 한 줄로 \"회수 노트: <위 메모리가 본 질문과 어떻게 "
    "관련되는가, 무관하면 '무관'>\" 명시 출력 의무. 회수 fact 와 답변이 "
    "모순되면 즉시 표기."
)
```

를 다음으로 교체:

```python
CONTRACT = (
    "답변 시작 전 한 줄로 \"회수 노트: <위 메모리가 본 질문과 어떻게 "
    "관련되는가, 무관하면 '무관'>\" 명시 출력 의무. 회수 fact 와 답변이 "
    "모순되면 즉시 표기. 옵션·권장·다음 단계 제시 시 위 feedback·project "
    "메모리의 명시 룰과 충돌하는 항목은 제거하거나 \"회수 메모리 X 위반 "
    "가능성\"으로 표기."
)
```

- [ ] **Step 4: `hooks/memory-recall.py` `_format_output` 인라인 문자열 동일 교체**

`hooks/memory-recall.py`의 `_format_output` 끝 (현재):

```python
    lines.append(
        "답변 시작 전 한 줄로 \"회수 노트: <위 메모리가 본 질문과 어떻게 "
        "관련되는가, 무관하면 '무관'>\" 명시 출력 의무. 회수 fact 와 답변이 "
        "모순되면 즉시 표기."
    )
```

를 다음으로 교체 (`recall_core.CONTRACT` 와 byte-동일):

```python
    lines.append(
        "답변 시작 전 한 줄로 \"회수 노트: <위 메모리가 본 질문과 어떻게 "
        "관련되는가, 무관하면 '무관'>\" 명시 출력 의무. 회수 fact 와 답변이 "
        "모순되면 즉시 표기. 옵션·권장·다음 단계 제시 시 위 feedback·project "
        "메모리의 명시 룰과 충돌하는 항목은 제거하거나 \"회수 메모리 X 위반 "
        "가능성\"으로 표기."
    )
```

> 두 문자열은 토큰화 후 byte-동일해야 한다 (continuation 경계의 trailing space 주의). parity 테스트가 한쪽만 바뀌면 잡는다. (※ 최초 구현 당시 경계는 `"feedback·project "`/`"X 위반 "` 였으나 audit R2E-1 정정으로 현재 코드 경계는 `"위 회수 메모리에 "` + `"명시된"`, `"<이름> 위반 "` + `"가능성"` — 최종 계약 문구는 design D3 참조.)

- [ ] **Step 5: 테스트 통과 확인**

Run: `python3 -m pytest tests/test_recall_core_parity.py::test_self_check_clause_present_and_parity -v`
Expected: PASS

- [ ] **Step 6: 회귀 확인 + 커밋**

Run: `python3 -m pytest tests/test_recall_core_parity.py tests/test_memory_hook.py tests/test_compact_reinjection.py -q`
Expected: PASS (`test_memory_hook.py:200-201`은 `"MEMORY CONTEXT ("`/`"회수 노트:"` 만 단언 → 불변; compact 재주입은 `format_memory_context` 경유라 조항 자동 전파, `test_compact_reinjection`은 system-reminder 블록 스킵만 검사)

```bash
git add src/recall_core.py hooks/memory-recall.py tests/test_recall_core_parity.py
git commit -m "feat(effective-recall): 회수 계약에 self-check 조항 추가 (옵션·권장 직전 feedback·project 룰 cross-reference, byte-parity)"
```

---

### Task 2: self_eval strict-cited 목표 게이트 (`recall_utilization_gate` + `--target`)

**Files:**
- Modify: `src/self_eval.py` (`recall_utilization` 함수 정의 다음, line ~1060 부근에 상수+함수 추가; argparse/main 분기 ~1198·~1244 수정)
- Test: `tests/test_self_eval.py`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_self_eval.py` 맨 끝(마지막 `class` 다음, 파일 끝)에 추가:

```python
class TestRecallUtilizationGate(unittest.TestCase):
    def test_pass_at_or_above_target(self):
        from self_eval import recall_utilization_gate
        util = {
            "by_status": {"cited": 6, "marker_only": 4, "unused": 20, "no_response": 5},
            "utilization_rate_strict": 0.20,
        }
        g = recall_utilization_gate(util, target=0.15, min_judged=30)
        self.assertTrue(g["pass"])
        self.assertEqual(g["judged"], 30)      # 6+4+20, no_response 제외
        self.assertEqual(g["target"], 0.15)

    def test_fail_below_target(self):
        from self_eval import recall_utilization_gate
        util = {
            "by_status": {"cited": 3, "marker_only": 4, "unused": 30, "no_response": 0},
            "utilization_rate_strict": 0.081,
        }
        g = recall_utilization_gate(util, target=0.15, min_judged=30)
        self.assertFalse(g["pass"])
        self.assertIn("<", g["reason"])

    def test_insufficient_sample(self):
        from self_eval import recall_utilization_gate
        util = {
            "by_status": {"cited": 1, "marker_only": 1, "unused": 5, "no_response": 0},
            "utilization_rate_strict": 0.14,
        }
        g = recall_utilization_gate(util, target=0.15, min_judged=30)
        self.assertFalse(g["pass"])           # 표본 부족 → fail (목표 근접해도)
        self.assertIn("insufficient_sample", g["reason"])

    def test_default_target_constant(self):
        import self_eval
        self.assertEqual(self_eval.RECALL_UTILIZATION_TARGET, 0.15)
        self.assertEqual(self_eval.RECALL_UTILIZATION_MIN_JUDGED, 30)
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `python3 -m pytest tests/test_self_eval.py -k RecallUtilizationGate -v`
Expected: FAIL (`ImportError: cannot import name 'recall_utilization_gate'`)

- [ ] **Step 3: 상수 + 게이트 함수 구현**

`src/self_eval.py`에서 `recall_utilization(` 함수 정의가 끝나는 지점(현재 `return { ... "per_event_sample": per_event[:10], }` 직후, `def _percentile` 앞 line ~1060)에 추가:

```python
# NEXT-38 Phase 1② — strict cited 목표 게이트 (spec §4.3②, 설계 D4/D5).
# strict cited 는 substring-match lower bound 라 100% 불가(§4.4). 2× baseline
# 7.62% → 0.15 목표. judged 표본 부족 시 noise 회피 위해 fail(insufficient).
# 측정 로직 자체는 불변 — 게이트는 판정만 얹는다(자동 튜닝 안 함, D6).
RECALL_UTILIZATION_TARGET = 0.15
RECALL_UTILIZATION_MIN_JUDGED = 30


def recall_utilization_gate(
    util_result: dict,
    target: float = RECALL_UTILIZATION_TARGET,
    min_judged: int = RECALL_UTILIZATION_MIN_JUDGED,
) -> dict:
    """recall_utilization() 결과를 strict cited 목표 대비 pass/fail 판정.

    judged = cited + marker_only + unused (no_response 제외 — recall_utilization
    의 judged 정의와 동일). judged < min_judged 면 'insufficient_sample'
    (pass=False) — 소표본 noise 로 게이트가 흔들리는 것을 차단.

    반환: {pass, strict, target, judged, min_judged, reason}
    """
    by = util_result.get("by_status", {}) or {}
    judged = (
        by.get("cited", 0) + by.get("marker_only", 0) + by.get("unused", 0)
    )
    strict = util_result.get("utilization_rate_strict", 0.0)
    if judged < min_judged:
        return {
            "pass": False,
            "strict": strict,
            "target": target,
            "judged": judged,
            "min_judged": min_judged,
            "reason": f"insufficient_sample (judged={judged} < {min_judged})",
        }
    passed = strict >= target
    return {
        "pass": passed,
        "strict": strict,
        "target": target,
        "judged": judged,
        "min_judged": min_judged,
        "reason": f"strict {strict:.4f} {'>=' if passed else '<'} target {target:.4f}",
    }
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `python3 -m pytest tests/test_self_eval.py -k RecallUtilizationGate -v`
Expected: PASS (4건)

- [ ] **Step 5: CLI `--target` 플래그 배선**

`src/self_eval.py`의 argparse 블록에서 `--recall-utilization` 인자 정의 다음(현재 line ~1197, `--source` 인자 앞)에 추가:

```python
    parser.add_argument(
        "--target",
        type=float,
        default=RECALL_UTILIZATION_TARGET,
        help="--recall-utilization strict cited 목표치 (default %(default)s, "
             "Phase 1② 완료 게이트 — spec §4.3②)",
    )
```

그리고 `if args.recall_utilization:` 분기(현재 line ~1244)에서 `json.dump` 전에 gate 부착:

```python
        if args.recall_utilization:
            out = recall_utilization(
                metrics_path=args.metrics,
                projects_root=args.projects_root,
                hours_back=args.hours,
                use_cache=args.use_cache,
                events_source=args.source,
            )
            out["gate"] = recall_utilization_gate(out, target=args.target)
            json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
            return 0
```

- [ ] **Step 6: CLI smoke 확인 + 회귀 + 커밋**

Run (CLI 배선 smoke — gate 키 출력 확인):
```bash
python3 src/self_eval.py --recall-utilization --target 0.15 --json | python3 -c "import sys,json; d=json.load(sys.stdin); print('gate' in d, d.get('gate',{}).get('reason'))"
```
Expected: `True <reason 문자열>` (데이터 적으면 `insufficient_sample`, 정상)

Run (회귀): `python3 -m pytest tests/test_self_eval.py -q`
Expected: PASS

```bash
git add src/self_eval.py tests/test_self_eval.py
git commit -m "feat(effective-recall): self_eval strict-cited 목표 게이트 (recall_utilization_gate + --target, 목표 0.15)"
```

---

### Task 3: e2e — 새 계약이 self_eval ingestion·sanitize·회수노트 회귀 안 깸 + 전체 회귀 + 측정 절차

**Files:**
- Test: `tests/test_recall_core_parity.py`

- [ ] **Step 1: ingestion 회귀 테스트 작성**

`tests/test_recall_core_parity.py` 맨 끝에 추가. 새 계약 footer 가 `self_eval`의 회수 name 추출(`RECALLED_NAME_RE` 기반)·header 인식·sanitize 를 깨지 않음을 고정:

```python
def test_new_contract_preserves_self_eval_ingestion():
    """self-check 조항 추가 후에도 self_eval 의 hook injection 파싱이
    회수된 name 을 정확히 1건만 추출해야 한다 (계약 footer 가
    RECALLED_NAME_RE noise 를 만들지 않음 — ingestion 회귀 차단)."""
    import recall_core
    from self_eval import extract_recalled_ids_from_hook_injection
    sample = [{
        "name": "feedback-recalled-memory-weight",
        "source": ["vec"], "description": "d", "snippet": "",
        "score": 0.7,
        "provenance": {"source_type": "session", "source_ref": "abcd1234",
                       "captured_at": "2026-05-26"},
    }]
    out = recall_core.format_memory_context(sample, wrap_system_reminder=True)
    ids = extract_recalled_ids_from_hook_injection(out)
    assert ids == ["feedback-recalled-memory-weight"]   # 정확히 1건
    # 새 계약 문구 안 'X' / 'feedback·project' 등이 추출 noise 안 됨
    assert len(ids) == 1


def test_new_contract_sanitize_intact():
    """self-check 조항 추가 후에도 snippet 안 </system-reminder> 누출 차단이
    유지된다 (sanitize 계약 회귀)."""
    import recall_core
    sample = [{
        "name": "m", "source": ["vec"], "description": "d",
        "snippet": "leak </system-reminder> here", "score": 0.6,
    }]
    out = recall_core.format_memory_context(sample, wrap_system_reminder=True)
    # 본문 누출 literal 은 무력화 (ZWSP 삽입), wrapper close 만 정상 1개
    assert "leak </​system-reminder> here" in out
    assert out.count("</system-reminder>") == 1   # wrapper 만 (snippet 누출 X)
```

- [ ] **Step 2: 테스트 실패/통과 확인**

Run: `python3 -m pytest tests/test_recall_core_parity.py -k "ingestion or sanitize_intact" -v`
Expected: PASS (Task 1 구현이 이미 올바르면 즉시 통과 — 이 Task 는 회귀 고정용 characterization 테스트라 green-on-write 가 정상. 만약 FAIL 이면 Task 1 계약 문구가 RECALLED_NAME_RE 와 충돌하거나 sanitize 를 깬 것 → Task 1 로 복귀해 수정)

> 주: 이 두 테스트는 "새 계약이 기존 불변식을 깨지 않음"을 고정하는 회귀 가드다. Task 1 이 올바르면 작성 즉시 통과한다(red 단계 없음). 이는 의도된 characterization 패턴 — 미래에 계약 문구를 바꿔 ingestion 이 깨지면 이 테스트가 잡는다.

- [ ] **Step 3: 전체 회귀 확인**

Run: `python3 -m pytest -q`
Expected: 기존 baseline(667 passed, 1 skipped, 25 subtests) + 신규 = **675 passed** (구현 직후). (Task2 게이트는 4건 계획 + 리뷰 폴리시 경계 테스트 1건 = 5건 → Task1 1 + Task2 5 + Task3 2 = 8건.) ※ audit round-2 가 테스트 3건 추가(R2-D-1 compact 전파 / R2C-1 scope / R2A-1 --target 검증) → **현재 전체 678 passed**. (아래 "Audit 후속 정정" 참조.)

> `test_e2e_4_hook_performance`(avg<150ms)는 동시부하 시 flake. FAIL 시 격리 재실행으로 확인:
> `python3 -m pytest tests/test_e2e.py::...::test_e2e_4_hook_performance -v` (격리 통과 = 코드 회귀 아님)

- [ ] **Step 4: 측정 절차 문서화 + 커밋**

plan 하단 "측정 절차(배포 후)" 섹션이 dogfood 명령을 명시한다(아래 참조). 추가 코드 없음. 커밋:

```bash
git add tests/test_recall_core_parity.py
git commit -m "test(effective-recall): 새 계약의 self_eval ingestion·sanitize 회귀 가드 + e2e"
```

---

## 완료 게이트 (설계 §5 / spec §4.3② 대응)

- [ ] self-check 계약 문구가 양 포맷터에 추가되고 byte-parity 유지 (Task 1)
- [ ] `recall_utilization_gate()` + `--target` 로 strict cited 목표(0.15) 대비 pass/fail 판정 가능 (Task 2)
- [ ] 새 계약이 `self_eval` ingestion·sanitize·"회수 노트:" 회귀를 깨지 않음 + 전체 회귀 통과 (Task 3)
- [ ] 확정 목표 수치: **strict cited ≥ 0.15** (judged ≥ 30, ≥1주 dogfood). 보조: unused bucket 비중 감소.

## 측정 절차 (배포 후 — 배포는 형 승인 영역)

계약 배포(install.sh 재배포 — 형 승인) 후 ≥1주 누적되면:

```bash
# strict cited 목표 대비 판정 (gate.pass / gate.reason 확인)
python3 src/self_eval.py --recall-utilization --target 0.15 --json

# 보조: 168h(1주) 윈도우 + transcript 소스로 재구축 비교
python3 src/self_eval.py --recall-utilization --target 0.15 --source transcripts --hours 168 --json
```

판정 해석:
- `gate.pass=true` → 완료 게이트 ② 충족 (strict ≥ 0.15, judged ≥ 30)
- `gate.reason="insufficient_sample..."` → 더 누적 필요 (게이트 미평가)
- `gate.pass=false` + strict < 0.15 → 목표 미달. 단 strict 는 lower bound(§4.4)이므로 `by_status.unused` 감소·`utilization_rate_lenient` 상승을 함께 보고 보강 판단. 목표 수치는 `--target` 으로 형이 조정 가능.

## 비범위 (별도 / 형 승인)

- ③ stale 자동 감지 (코드/모델명/버전 참조 재검증, BGE→Arctic 회귀 케이스) — 별도 plan.
- 회수 임계값 자동 튜닝 (D6) — 영구 미구현.
- CLAUDE.md "회수 알림 규칙" 보강 (D1 보조 옵션) — 형 승인 후 선택.
- install.sh 재배포(계약 활성화)·GitHub push/tag/release — 형 승인 영역.

---

## Audit 후속 정정 (2026-05-31, round 2~3 다차원 adversarial 점검)

머지 후 6+5+5개 렌즈 × 2인 독립검증 워크플로 3라운드로 전체를 재점검. **코드 correctness/parity/integration 결함 0건**, 아래 정정.

> **⚠️ 단일 진실원천**: 최종 계약 문구·게이트 사양은 `docs/specs/2026-05-31-phase1-effective-recall-design.md` 의 D2/D3/§3(audit 후 갱신)이다. **본 plan 의 Task 1~3 코드블록과 그 안팎 prose(Goal·Architecture·byte-parity 주석·테스트 코드 등)는 "최초 구현 당시" 기록이라 R2E-1 이전의 `feedback·project`/`"X"` 계약을 보여줄 수 있다 — 이는 모두 아래 R2E-1 정정으로 대체됐다.** plan 을 재실행 시 design D3 의 현행 계약을 사용할 것.

### round 2 (4건 정정 + 1 not-a-bug)

- **R2E-1 (important)** — 계약의 `feedback·project` type-scoping 이 회수 출력에 비가시(렌더 `[name]` 은 frontmatter title, 78% 가 type 글자 부재). 계약을 **content+이름 기반**으로 재서술: `위 회수 메모리에 명시된 룰·제약` + `"회수 메모리 <이름> 위반 가능성"`(round-1 "X" placeholder 모호성도 동시 해결). 양 포맷터 동시 수정, byte-parity 유지. (`src/recall_core.py`, `hooks/memory-recall.py`, `tests/test_recall_core_parity.py`)
- **R2C-1 (minor, disclosure)** — 게이트 `strict` 는 Layer-4 hook 회수면만 측정(compact 재주입 제외 — 측정 인프라가 Phase1② 이전부터의 한계, D6 동결). 게이트 출력에 `scope` 키 추가로 과대인증 방지 + 설계 §3 한계 명시. (`src/self_eval.py`)
- **R2A-1 (minor)** — `--target nan/inf` 가 비-스펙 JSON(NaN/Infinity bare token) 출력. argparse `_target_arg` 로 [0,1] 유한 실수만 통과. (`src/self_eval.py`, `tests/test_self_eval.py`)
- **R2-D-1 (minor, test 갭)** — D7 의 "compact 재주입이 CONTRACT 자동 전파" 주장에 직접 테스트 없음 → `test_self_check_clause_propagates_to_compact_intro` 추가(실제 `COMPACT_INTRO` 로 렌더 검증). (`tests/test_recall_core_parity.py`)
- **R2C-2 (not-a-bug)** — 게이트가 `utilization_rate_strict` 를 재계산 없이 신뢰하는 설계가 dict incoherent 시 판정 뒤집힐 수 있으나, 유일 producer 인 `recall_utilization` 이 항상 coherent 산출 → shipped 경로 unreachable. 수정 안 함.

### round 3 (코드 결함 0건 — dry 수렴 확인. doc-sync 2건만)

round-3 5렌즈(수정검증·신계약심층·잔여hunt·문서정합·completeness) × 2인 검증에서 **코드/parity/integration/CLI 신규 결함 0건**. R2E-1 reword 가 남긴 plan prose stale 2건만 정정:
- **R3D-1 (minor, doc)** — plan Goal(line 5)·byte-parity 주석이 폐기된 `feedback·project`/`"X"` 계약을 현행처럼 서술 → content+이름 기반으로 정정 + 상단 disclaimer 범위를 prose 전체로 확대.
- **R3D-2 (minor, doc)** — Task3 Step3 회귀 예상치 `675 passed` 가 round-2 추가 3건 미반영 → **678 passed** 로 갱신.
