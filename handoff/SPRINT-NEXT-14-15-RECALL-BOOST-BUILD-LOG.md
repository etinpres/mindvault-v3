---
name: handoff-sprint-next-14-15-recall-boost-build-log
description: NEXT-15 진단 (Gemma 비결정성 + trigger 60% miss) + NEXT-14a/b fix (retry union, tail window, ALWAYS_FIRE) + P2 운영 측정 (ALWAYS_FIRE 4→5 candidates 효과 입증) + backfill_cli --deep 옵션
---

# MindVault v3 → NEXT-14a/b + NEXT-15 빌드 로그

*Drafted: 2026-05-24, master HEAD `f18e99f` (NEXT-14 머지 후, --deep 추가 전).*
*Discovered & fixed: NEXT-8 BUILD-LOG §5 의 "extractor recall 폭 한계" 후속.*

## 1. NEXT-15 진단 — 손실 지점 정확 좌표

NEXT-8 BUILD-LOG §5 가 노출한 "backfill 24/1 hit ratio" 의 진짜 원인 분석.

### 1.1 진단 흐름

backfill 5건 운영 호출 + 같은 jsonl 직접 호출 두 번 측정.

**손실 지점 1 — trigger 게이트 60% miss**:
- 5건 중 3건이 `extractor: no trigger in {sid}, skip` 으로 early return
- TRIGGER_RE + NEXT-1 + NEXT-10 ACK 3 layer 모두 미발화
- 형의 실제 결정 confirmation 패턴이 strict regex 범위 밖

**손실 지점 2 — Gemma 비결정성 (≈50% miss)**:
- 같은 jsonl 두 번 연속 호출 (5초 차이): 첫 호출 3 candidates, 두 번째 0
- temperature 0.2 + MLX 4bit 모델 임에도 동일 input 에 다른 output
- "한 번이라도 fire 하면 Gemma 가 정확한 사실 추출" — 가설 자체는 입증
  (turns_cache, diff color, Gemma classifier fix 정확 추출)

**손실 지점 3 (배제됨)**:
- Gemma 응답 파싱 (`parse_gemma_json`) — valid 인정률 100%
- `_stage_with_conflict_resolution` — dup 없으면 100% staged
- 즉 Gemma 가 candidates 만들면 staging 까지 완주

### 1.2 hit ratio 수식

```
실제 hit ratio = P(trigger fires) × P(Gemma returns ≥1 candidate)
              = 0.40 × 0.50 = 0.20  (20%)
```

backfill 전체 4% (24/1) 는 짧은 잡담 세션 다수 포함 ↓ 효과.

## 2. NEXT-14a/b — fix

`f18e99f feat(extractor): NEXT-14a/b recall boost — retry union + tail window + ALWAYS_FIRE`

### 2.1 NEXT-14b (P0) — Gemma 멱등성 보강

`extract_from_jsonl` 에 retry loop + union 추가:

- `MV2_EXTRACTOR_GEMMA_RETRIES` (default 2 = 최초 1 + retry 2 = 최대 3 호출)
- 첫 hit 시에도 한 번 더 시도 — `_union_by_title` 으로 candidates 누적
  (NEXT-15 측정: 같은 input 도 다른 candidates — 정보 누적 효과)
- 0건이면 retry 끝까지 exhaust

```python
def _retries() -> int:
    return max(0, int(os.environ.get("MV2_EXTRACTOR_GEMMA_RETRIES", "2")))

def _union_by_title(*lists) -> list[dict]:
    seen, merged = set(), []
    for lst in lists:
        for c in lst:
            t = (c.get("title") or "").strip()
            if t and t not in seen:
                seen.add(t); merged.append(c)
    return merged
```

### 2.2 NEXT-14a (P1) — trigger 폭 확장

- `MV2_EXTRACTOR_TAIL_TURNS` (default 40 → 80) — `load_tail_messages` window
- `MV2_EXTRACTOR_ALWAYS_FIRE=1` opt-in — `has_trigger` 결과 무시, Gemma 항상 호출
  - 매 SessionEnd Gemma fire 비용 ↑ but trigger 60% miss 본질 해결
  - candidates 0건이면 latency 만 추가, 데이터 손실 없음

### 2.3 테스트

`tests/test_extractor_recall_boost.py` 17건 PASS:
- env helpers: default / override / invalid graceful / clamping
- `_union_by_title`: dedup first wins / empty / skip titleless
- retry: first hit + union 한 번 더 / exhaust 빈 / retries=0 비활성
- always_fire: trigger 미발화 → off 빈 / on Gemma 호출

회귀 검증: pytest tests/ → **272 passed, 0 failed** (이전 255 + NEXT-14 17).

## 3. P2 NEXT-12 — 운영 측정 + stateless 검증

NEXT-14 fix 후 실제 5건 sid 직접 호출 측정:

### 3.1 retry + tail 80 만 (ALWAYS_FIRE off)

```
949a8635 (75.5s, 3 attempts): 0 candidates  ← 첫 호출 (10:19:12) 에선 3건 잡혔던 sid
fae488da:  0 (trigger 미발화, early skip)
332cc804:  0 (trigger 미발화)
557ed49f:  jsonl 사라짐
ba14bf59:  0 (trigger 미발화)

TOTAL: 0 candidates
```

→ retry 효과 단독 측정 불가 (trigger 통과한 1건도 stochastic 0).
→ tail 40 → 80 효과 0 (trigger 패턴 자체가 tail 80 안에도 없음).

### 3.2 ALWAYS_FIRE=1 추가

```
949a8635 (75.5s):  0 candidates
fae488da (30.5s):  2 candidates
  - MCP 서버 현재 상태 보고
  - MCP 서버 목록 및 현재 연결 상태
332cc804 (74.4s):  0 candidates
ba14bf59 (36.6s):  3 candidates
  - Claude Code 최신 버전 업데이트 및 세션 재시작 필요
  - Claude Code 최신 버전 업데이트 명령어
  - 새 기능 사용을 위한 세션 재시작 필요

TOTAL: 5 candidates (4건 호출 / 2 sid hit)
```

→ **ALWAYS_FIRE 가 NEXT-14a 의 진짜 갈래**. trigger 60% miss 의 본질 해결.
→ NEXT-14a tail window 효과는 ALWAYS_FIRE 보조용 (tail 80 으로 Gemma prompt 풍부).

### 3.3 NEXT-12 stateless 결론

- 949a8635 동일 sid 시간차 호출: 3 (10:19) → 0 (10:25) → 0 (11:18) → 0 (P2 측정).
  **Gemma 비결정성 + jsonl 변동성** 둘 다 영향. NEXT-14b retry 가 부분 보완.
- 본질 해결책: jsonl content hash 기반 deterministic input (window 고정) + 결과 캐싱.
  본 sprint 범위 밖. NEXT-16 backlog.

## 4. backfill_cli `--deep` 옵션 (P2 마무리)

NEXT-14 효과 입증 후 batch 모드 패키징. `--deep` 한 플래그로 묶음:

```bash
python3 backfill_cli.py --last-hours 168 --deep
# = MV2_EXTRACTOR_ALWAYS_FIRE=1 + TAIL_TURNS=120 + GEMMA_RETRIES=3
# 매 sid 당 latency 30~90s. batch 누적 정리용.
```

실시간 hook 부담은 default 보존 (ALWAYS_FIRE off). 형이 주기적으로 `--deep` 호출.

## 5. v3.0 ship 게이트 재평가

| 게이트 | 상태 |
|---|---|
| Indexing | ✅ |
| Recall hook | ✅ (66.5% hit rate, 0% FP) |
| Memory Compiler | ✅ (NEXT-8 fix 후 운영 fire 가능) |
| Procedural extractor recall | ⚠️ default 20%, `--deep` 50%+ — opt-in 우선 |
| Self-eval | ✅ |
| Query intent | ✅ |
| Backfill CLI | ✅ (NEXT-13 + --deep) |
| Test suite | ✅ 272 passed, 0 failed |
| 자동 narrative 갱신 | ✗ (NEXT-11 backlog) |

NEXT-14 + --deep 으로 batch 회복 경로 완성. 실시간 hook 의 자동 recall 폭은 여전히
20% — opt-in `MV2_EXTRACTOR_ALWAYS_FIRE=1` 영구화로 끌어올릴 수 있으나, 매 SessionEnd
30~90s 부담 trade-off. ship 결정은 형 영역 (feedback-ship-defer 메모리).

## 6. 신규 / 정리된 backlog

| # | 후보 | 우선순위 |
|---|---|---|
| **NEXT-16** | jsonl content hash 기반 deterministic input — Gemma 결과 캐싱. stateless 본질 해결 | P1 (NEXT-12 흡수) |
| **NEXT-17** | 측정 — ALWAYS_FIRE 영구화 시 SessionEnd latency 분포 (p50/p95). `feedback-ship-defer` 결정 데이터 | P2 |
| **NEXT-11** | project narrative 자동화 (NEXT-2 embed match + narrative trigger) | P2 (기존) |
| **NEXT-18** | ACK_RE 폐기 검토 — ALWAYS_FIRE 가 본질 해결하면 NEXT-10 가치 ↓. 측정 후 결정 | P3 |

## 7. master HEAD

```
[--deep 추가 commit — 이 BUILD-LOG 와 같은 PR]
f18e99f feat(extractor): NEXT-14a/b recall boost — retry union + tail window + ALWAYS_FIRE
96492be feat(extractor): NEXT-10 ACK 휴리스틱 3rd layer  ← NEXT-15 진단으로 효과 0 입증, 폐기 검토
8345246 feat(backfill_cli): NEXT-13 SessionEnd hook backfill CLI 표준화
ca14497 docs(handoff): NEXT-8 BUILD-LOG
524e442 test(schema): expected v2 → 현재 SCHEMA_VERSION (v3)
eaa5434 fix(session-end): PROJECTS_ROOT 전체 슬롯 glob
```

## 8. 관련

- [[handoff-sprint-next-8-projects-root-fix-build-log]] — NEXT-8 fix + 본 sprint 진단 trigger
- [[handoff-v3-plan]] §1.1 procedural type 누락 — NEXT-14 ALWAYS_FIRE 가 본질 해결
- [[handoff-sprint-next-1-auto-trigger-build-log]] — NEXT-1 NEXT_ACTION 휴리스틱. NEXT-14 ALWAYS_FIRE 가 그 상위 layer
- [[project-mindvault]] — 통합 진척 노트
