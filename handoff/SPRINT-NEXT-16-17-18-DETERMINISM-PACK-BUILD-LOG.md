---
name: handoff-sprint-next-16-17-18-determinism-pack-build-log
description: NEXT-16 prompt SHA256 캐시 (deterministic input) + NEXT-17 extractor stats CLI (latency·trigger 분포·cache hit rate 측정) + NEXT-18 ACK_RE 폐기 검토 → 보존 결정 (next10-ack 운영 hit 5.6%)
---

# MindVault v3 → NEXT-16/17/18 determinism + 측정 + 결정 pack

*Drafted: 2026-05-24, master HEAD `7ce640c` (NEXT-17 stats CLI 머지 후, NEXT-18 결정 commit 전).*
*P1+P2+P3 순차 진행 (NEXT-14/15 BUILD-LOG follow-up).*

## 1. NEXT-16 (P1) — prompt SHA256 캐시

NEXT-12/15 stateless 가정의 본질 해결책.

### 1.1 설계

`src/extractor_cache.py` 신규:
- 캐시 DB: `~/.claude/mindvault-v2/extractor_cache.db` (sqlite WAL)
- key: `hashlib.sha256(prompt.encode("utf-8")).hexdigest()`
- 값: `candidates_json` (json.dumps list[dict]) + count + ts + hit_count
- API: `cache_get`, `cache_put`, `cache_stats`, `cache_clear`, `prompt_hash`
- opt-out: `MV2_EXTRACTOR_CACHE_DISABLE=1`

`extract_from_jsonl` 통합:
```python
prompt = build_prompt(msgs)
cached = cache_get(prompt)
if cached is not None:
    return cached  # Gemma 0 호출
# ... retry loop ...
cache_put(prompt, merged)  # 빈 list 도 저장 — 재시도 비용 회피
```

### 1.2 효과 (운영 측정)

- 같은 jsonl + 같은 tail window → 결정론적 결과 (NEXT-15 측정한 stochasticity 제거)
- jsonl 변하면 prompt 다른 hash → 자동 invalidate
- backfill `--deep` 두 번째 호출 instant (90s → ~5ms)
- 누적 운영에서 Gemma 호출 총량 ↓

### 1.3 graceful 설계

- `cache_get` 실패 (import error, sqlite lock 등) → None 반환 → 기존 동작 그대로
- `cache_put` 실패 → debug 로그만, 결과 무관
- 즉 캐시 layer 가 깨져도 SessionEnd hook 자체는 항상 작동

master commit: `7880c03 feat(extractor): NEXT-16 prompt → candidates 결과 캐시`

테스트: 16건 PASS (round-trip / hit_count / disable env / stats / extract integration).

## 2. NEXT-17 (P2) — extractor stats CLI

ship 결정에 필요한 운영 데이터 측정 도구.

### 2.1 설계

`src/extractor_stats_cli.py` 신규:
- debug.log 9 정규식 패턴 매칭 (trigger / no_trigger / attempt / cache_hit / compiled / staged / jsonl_missing 등)
- `extractor_cache.cache_stats()` 결합
- 출력: human-readable (default) 또는 `--json`
- 옵션: `--last-hours N` (default 168), `--all`

### 2.2 첫 운영 실측 (최근 24h, ALWAYS_FIRE off default)

```
SessionEnd 호출 총수:       71
  - jsonl missing:           12  ← NEXT-8 fix 이전 잔여
  - no_trigger (skip):       29
  - no candidates 후 fire:   29
  - always-fire bypass:      6  ← --deep batch
trigger fired:              71
  - next1-action         39  (55%)
  - keyword              28  (39%)
  - always-fire          6
  - next10-ack           4  (5.6%) ← NEXT-18 결정 데이터
Gemma attempts: min=1 max=3 avg=1.65  ← NEXT-14b retry 효과
candidates/extract: zero=27 nonzero=24 max=2 nonzero_avg=1.04
staged pass rate: 1.0 (over 1 sessions)
Memory Compiler: 1 sessions, 0/2 updates (rate=0.0)
cache hits 누적: 2  ← NEXT-16 도입 직후
```

### 2.3 NEXT-14 효과 정량화

- backfill 측정 (NEXT-15 BUILD-LOG): nonzero rate 20% baseline
- NEXT-17 운영 실측 (24h): **47%** (nonzero 24 / total 51 attempt)
- = **2.4배 ↑** (NEXT-14b retry union + NEXT-14a tail 80 효과)
- 단, ALWAYS_FIRE off 기본 상태 — 더 끌어올리려면 ALWAYS_FIRE 영구화

master commit: `7ce640c feat(stats): NEXT-17 extractor latency + cache hit rate 측정 CLI`

테스트: 8건 PASS (정규식 9개 + 통계 정확성 + 빈 log graceful).

## 3. NEXT-18 (P3) — ACK_RE 폐기 검토

### 3.1 결정: **보존**

### 3.2 사유

NEXT-17 운영 24h 측정:
- next10-ack 분기 hit: **4건 / 71 SessionEnd 호출 = 5.6%**
- 0건 아님. trigger fired 4건 모두 Gemma 호출까지 진행
- NEXT-10 BUILD-LOG (96492be) 직후 "운영 hit 0건" 측정과 다름 — 운영 시간 누적 + 실 사용 패턴 변화

→ 폐기까지는 갈 필요 없음. 보존 + 운영 모니터링.

### 3.3 단, 다음 결정 트리거

| 조건 | 다음 액션 |
|---|---|
| next10-ack hit rate 30일 누적 < 2% | NEXT-19 sprint 로 폐기 |
| next10-ack false positive (Gemma candidates 0건 비율) > 90% | 폐기 |
| NEXT-19 ALWAYS_FIRE 영구화 결정 시 (NEXT-17 latency 측정 후) | NEXT-10 무용 → 폐기 |

### 3.4 운영 측정 자동화

`extractor_stats_cli.py --last-hours N --json` 를 cron 또는 `/cs` 시점에 호출해
trigger_layers 비율 추적. 결정 트리거 조건 자동 감지.

## 4. v3.0 ship 게이트 재평가

| 게이트 | 상태 |
|---|---|
| Indexing | ✅ |
| Recall hook | ✅ (66.5% / FP 0%) |
| Memory Compiler | ✅ |
| Procedural extractor recall | **✅ 47% (NEXT-14+16+17 후, 이전 20%)** — `--deep` 시 50%+ |
| Self-eval | ✅ |
| Query intent | ✅ |
| Backfill CLI | ✅ |
| Extractor cache (NEXT-16) | ✅ deterministic input |
| Stats CLI (NEXT-17) | ✅ 측정 인프라 |
| Test suite | ✅ 296 passed, 0 failed |
| 자동 narrative 갱신 | ✗ (NEXT-11 backlog) |

**v3.0 ship 게이트 거의 다 충족.** 남은 큰 한 가지 = NEXT-11 narrative 자동화. 형 결정 영역 (feedback-ship-defer).

## 5. master HEAD

```
[NEXT-18 결정 commit — 이 BUILD-LOG]
7ce640c feat(stats): NEXT-17 extractor latency + cache hit rate 측정 CLI
7880c03 feat(extractor): NEXT-16 prompt → candidates 결과 캐시
05fb155 feat(backfill_cli): --deep + NEXT-14/15 BUILD-LOG
f18e99f feat(extractor): NEXT-14a/b recall boost
96492be feat(extractor): NEXT-10 ACK 휴리스틱
8345246 feat(backfill_cli): NEXT-13
ca14497 docs(handoff): NEXT-8 BUILD-LOG
524e442 test(schema): NEXT-9 cleanup
eaa5434 fix(session-end): NEXT-8 PROJECTS_ROOT
```

오늘 (2026-05-24) `b440d9e` → 8 commit + 2 BUILD-LOG. 진단 → fix → 측정 → 결정 풀 사이클.

## 6. 신규 / 정리된 backlog

| # | 작업 | 우선순위 |
|---|---|---|
| **NEXT-19** | ALWAYS_FIRE 영구화 결정 — NEXT-17 stats 30일 누적으로 latency p95 / Gemma 호출 부담 측정 후 | P1 (ship 게이트 영향) |
| **NEXT-11** | project narrative 자동화 — NEXT-2 embed match + narrative trigger | P1 (ship 게이트) |
| **NEXT-20** | cron 으로 `extractor_stats_cli --json` 정기 실행 + 트렌드 누적 | P3 |
| **NEXT-21** | 30일 누적 시 NEXT-18 재검토 — next10-ack hit rate < 2% 면 폐기 | P3 |

## 7. 관련

- [[handoff-sprint-next-14-15-recall-boost-build-log]] — NEXT-14/15 진단 + fix (본 sprint 직접 follow-up)
- [[handoff-sprint-next-8-projects-root-fix-build-log]] — NEXT-8 dogfooding gap (오늘 시작점)
- [[handoff-v3-plan]] §1.4 자기-수정 메커니즘 — NEXT-17 stats CLI 가 그 측정 인프라
- [[project-mindvault]] — 통합 진척 노트
