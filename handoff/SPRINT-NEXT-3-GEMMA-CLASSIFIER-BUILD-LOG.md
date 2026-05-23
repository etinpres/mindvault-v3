---
name: handoff-sprint-next-3-gemma-classifier
description: V3-NEXT-IMPROVEMENTS #3 — query_intent.classify_with_gemma 신규. rule-based 가 unknown 으로 떨어진 짧은 query (≤40자) 에 한해 Gemma 로 보강 분류. opt-in env MV2_GEMMA_INTENT=1, timeout 2s, 실패 시 graceful 폴백.
---

MindVault v3 → 차기 보강 #3 — Gemma 보강 classifier 빌드 로그

## 요약

V3-NEXT-IMPROVEMENTS #3 해결. Sprint 16 의 rule-based query_intent classifier 가 unknown 영역에서 borderline 잡담·메타를 그대로 회수 호출로 흘려보내는 문제를 Gemma 보강으로 막는다. 운영 안정성을 위해 default off — `MV2_GEMMA_INTENT=1` opt-in 일 때만 발동.

master HEAD `143d4ad` (NEXT-2 embedding 매칭) 기준 worktree `worktree-next-3-gemma-classifier` 에서 작업.

## 자율 결정 사유

- **default off (opt-in env)** — Memory Compiler 와 동일 패턴. Gemma 호출 latency 가 hook 의 critical path 추가 부담 (rule-based unknown 비율 측정 후 정식 on 결정). 본 sprint 는 인프라만 깔고 형이 활성화 시점 결정.
- **호출 조건 = unknown + len ≤ 40 + env on** — rule-based 가 명확히 잡은 건은 Gemma 우회 (cost). 짧은 query 한정 — 긴 query 는 작업 지시일 가능성 높고 Gemma 분류 신뢰도 떨어짐. 40자는 짧은 잡담·메타(예: "너 모델 뭐야", "오늘 뭐 했지", "뭐해") 의 분포 상한선 추정.
- **timeout 2s** — 4B MLX 가 max_tokens=8 + temperature=0 짧은 출력이라 실측 100~300ms 권장값. 2s 는 안전 마진 + 서버 부하 시 graceful timeout.
- **other 라벨은 None 처리** — Gemma 가 분류 못 하겠다는 의미. rule-based unknown 그대로 둬야 안전 (chat/meta 강제 적용은 위험).
- **graceful 폴백** — Gemma 응답 None/timeout/parse fail/invalid label 모두 None 반환. hook 의 기존 try/except 가 rule-based intent_obj 그대로 사용.
- **prompt 한 줄 분류 형식** — chat/meta/code/recall/other 5라벨. max_tokens=8, temperature=0 으로 결정적 + 최소 토큰. `_normalize_gemma_label` 이 첫 영문 토큰만 추출 → "**chat**", "chat\n", "CHAT" 모두 chat 으로 정규화.

## 변경 상세

### A. `src/query_intent.py`

- 새 상수: `GEMMA_INTENT_URL`, `GEMMA_INTENT_MODEL`, `GEMMA_INTENT_TIMEOUT=2.0`, `GEMMA_INTENT_MAX_LEN=40`, `ENABLE_GEMMA_INTENT_ENV="MV2_GEMMA_INTENT"`.
- `gemma_intent_enabled() -> bool`: env 가 정확히 `"1"` 일 때만 True. `auto_compile_enabled` 와 동일 패턴.
- `_call_gemma_intent(prompt_text) -> str | None`: localhost:8080 chat completion 호출. 실패 시 None + debug log.
- `_normalize_gemma_label(raw) -> str | None`: 첫 영문 토큰 lowercase. `_VALID_GEMMA_LABELS = {chat, meta, code, recall, other}` 안의 라벨만 반환.
- `classify_with_gemma(prompt) -> IntentResult | None`: 핵심 진입점. 빈/긴 prompt → None. Gemma 호출 → 라벨 정규화. other/None → None (rule-based 유지). chat/meta/code/recall → `IntentResult(label, 0.6, ["gemma:" + label])`.

### B. `hooks/memory-recall.py` 통합

intent 추출 직후 unknown + env on 일 때 Gemma 보강:

```python
intent_obj = classify(prompt)
if intent_obj.intent == "unknown" and gemma_intent_enabled():
    gemma_obj = classify_with_gemma(prompt)
    if gemma_obj is not None:
        intent_obj = gemma_obj
intent_label = intent_obj.intent
intent_match = list(intent_obj.matched)
if should_skip_recall(intent_obj):
    ...
```

기존 try/except 안에 들어가 Gemma fail 시에도 rule-based 결과로 작동.

## 측정 데이터

### query_intent 단독

```
22/22 PASS (0.03s)
신규 12건: TestGemmaIntent.*
기존 10건 보존
```

### 전체 회귀

```
212/214 PASS (test_install_uninstall 제외, 102s)
2 fail = test_schema_v2.* — master HEAD `143d4ad` 동일 pre-existing. 본 sprint 무관.
```

### latency 예상치

- 4B MLX gemma-4-e4b-it-4bit, max_tokens=8, temperature=0 → 실측 ~150ms (이전 NEXT-2 BUILD-LOG 의 호출 평균).
- 호출 조건이 `unknown + len ≤ 40` 이라 전체 hook 호출 중 10~20% 추정 발동. 평균 hook latency 증가 ~15~30ms 예상.

## 안전 정책 준수

- `indexer.full_rebuild()` 호출 없음.
- Sprint 10 트랜잭션 패턴 무변경.
- BGE plist / `bge_m3_server.py` 무변경.
- launchctl 서비스 무관.
- default off — 형이 명시적 `export MV2_GEMMA_INTENT=1` 해야 발동.
- Gemma 미응답·timeout 시 rule-based 폴백 (recall flow 유지).
- worktree 격리.

## 미해결 / 다음 #4~#7

- **운영 누적 metric** — opt-in 활성 후 hook 호출 분포(unknown 비율) 및 Gemma 분류 결과의 회수 skip 효과를 self_eval 측정해야 default-on 결정 가능.
- **#4 type 별 게이트, #5 diff UI, #6 slug conflict, #7 scan latency** — 본 sprint 다음 사이클에서 순차 진행.

## 변경 파일

```
src/query_intent.py                                       | +120 -1
hooks/memory-recall.py                                    | +10 -2
tests/test_query_intent.py                                | +112
handoff/SPRINT-NEXT-3-GEMMA-CLASSIFIER-BUILD-LOG.md       | 신규
```
