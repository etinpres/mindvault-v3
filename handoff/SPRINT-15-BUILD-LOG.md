---
name: handoff-sprint15-build-log
description: Sprint 15 build log — Self-evaluation Loop 측정 인프라. metrics.jsonl + Claude session JSONL 로 hit rate, internal effort, false positive rate, self-affirming memory 4가지 metric batch 계산. 자동 게이트 조정 미구현(위험성)
---

MindVault v2 → v3 Sprint 15 — Self-evaluation Loop 빌드 로그

## 요약

V3-PLAN §3.C 의 4가지 metric (hit rate, internal effort, false positive rate,
자기충족 메모리 감지) 첫 측정 인프라. metrics.jsonl + projects/\*/\*.jsonl 합쳐
batch 분석. master HEAD `c03b7be` (Sprint 14) 기준.

자동 게이트 조정은 **의도적으로 미구현** — V3-PLAN §7 위험 매트릭스의 "Self-eval
Loop 가 잘못 학습 → 게이트 망가짐" 위험이 더 큼. metric 노출만 하고 형이 수동
조정. Sprint 16 query intent classifier 가 더 자연스러운 false positive 차단
메커니즘이라 자동 게이트 조정은 v3.0 ship 시점까지 보류.

## 자율 결정 사유

- **batch CLI 패턴** — `src/self_eval.py` 가 CLI 단발. hook 단에 실시간 분석
  안 붙임. 이유: 매 user prompt 마다 metric 계산이면 hook latency +수십 ms,
  토큰 낭비 (`no-v1-token-waste` 메모리 원칙). 분석은 형이 `python3 self_eval.py`
  로 필요할 때만.
- **자동 게이트 조정 미구현** — false positive count 임계 초과 시 `raw_cosine_min`
  자동 상향 안 함. 한 번 잘못 학습되면 도메인 hit 까지 차단되는 회복 불능 상태
  진입. Sprint 16 의 query intent classifier (mid-cosine zone discriminator)가
  본질적 false positive 차단 메커니즘이라 자동 조정 불필요.
- **negative cue 한국어 우선** — 형 사용 언어 분포 따라. 영어 cue 는 추후 추가.
  현재 regex: "관계없", "엉뚱한", "왜 .* 회수", "이거 아니야", "필요 없", 등.
- **self-affirming 임계 2 hits** — 본문에 affirming 키워드 1회는 흔한 진술,
  2회 이상이면 echo chamber 패턴. 실측 결과 8건 후보 모두 spot-check 적절.

## 변경 상세

### A. `src/self_eval.py` 신규 (320 lines)

핵심 API:

```python
analyze_recent(metrics_path, projects_root, hours_back=168) -> dict
load_recall_events(metrics_path, since_unix=None) -> list[dict]
load_turns(jsonl_path) -> list[dict]
measure_post_recall(turns, recall_ts) -> dict
has_negative_cue(text) -> bool
is_self_affirming(text, min_hits=2) -> bool
scan_self_affirming_memories(memory_dirs=None) -> list[dict]
format_report(summary) -> str
```

CLI: `python3 src/self_eval.py [--hours 168] [--json]`

내부:
- `_parse_ts` 가 Z 와 naive ISO 둘 다 처리. Z → UTC, naive → local tz astimezone (production
  metrics.jsonl 은 hook 가 `time.strftime` 으로 작성한 naive — local).
- `measure_post_recall(turns, recall_ts)`: recall ts ± 30분 window 의 turn 중 ts 직후
  ~ 다음 user turn 전까지 assistant tool_use 카운트.
- `analyze_recent`: 전체 jsonl turn 로드 (~14만 turn 추정, ~50MB) → recall event 각각의
  ±30분 window slice. 단일 batch 면 OK, 매 hook 호출이면 비용 큼 (실시간 미사용 이유).

### B. 테스트 (`tests/test_self_eval.py` 신규, 19 tests)

| TestCase | 검증 |
|---|---|
| TestParseTs | Z/naive/invalid 처리 |
| TestNegativeCue | 8 positive + 5 negative case |
| TestSelfAffirming | 임계값 2 hits |
| TestLoadRecallEvents | recall 필터·정렬·missing file |
| TestLoadTurns | system-reminder 제외·tool_use 추출·non-user/assistant skip |
| TestMeasurePostRecall | tool_use 카운트·next_user 식별 |
| TestScanSelfAffirming | memory_dirs 격리 |
| TestAnalyzeRecentIntegration | end-to-end (metrics + jsonl) |
| TestFormatReport | hit rate·self-affirming 표시 |

19/19 PASS (0.10s).

## 측정 데이터 — production 실측 (최근 168h, 2026-05-23)

```
# MindVault Self-eval Report
window: 최근 168h, total_recalls=439
hit rate: 66.5% (292/439)
avg internal effort (tool_use after recall): 0.60
false positive rate: 0.0% (0/39, 표본=다음 user turn 식별 가능한 recall)

self-affirming memory 후보: 8건
  - MindVault v1 (폐기) / v2 (운영) (5 hits) — ['완성', '운영 중', '품질 양호']
  - /assemble V4 Spike IX — shipper ★ bundle (4 hits) — ['ship-ready']
  - project-assemble-v4-spikes-index (3 hits) — ['완성']
  - /assemble V4 Spike XII — /assemble eject command (2 hits) — ['완성']
  - HyperFrames 롱폼 파이프라인 (Remotion 교체) (2 hits) — ['문제 없', '완성']
  (... 3건 추가)
```

### V3-PLAN §5 metric 표 v3 target 실측 비교

| Metric | v2.9 baseline | v3 target | v3 실측 (Sprint 15) |
|---|---|---|---|
| hook hit rate | ~79% (도메인 query 기준 추정) | 90%+ | 66.5% (전 호출 기준) |
| false positive rate | 미측정 | <5% | **0.0%** ✓ (39 표본) |
| Claude internal effort (avg tool_use) | 미측정 | <1 | **0.60** ✓ |
| procedural memory coverage | 0% | 70%+ | 0% (Sprint 13 슬롯만, 자동 추출은 다음 단계) |
| 자기 모순 메모리 감지율 | 0% | 80%+ | **8건 후보 탐지** (수동 검토 대상화 완료) |
| session-end auto compile latency | N/A | <10s | 미측정 (opt-in 단계) |

### hit rate 66.5% 해석

v2.9 BUILD-LOG 의 79% 보다 낮음 — 모든 호출 기준 (잡담·메타 포함) 으로 변경한 영향.
잡담은 raw_cosine_min=0.40 게이트가 picked=0 으로 처리 → 분모에 포함되지만 분자엔 안 들어감.
Sprint 16 query intent classifier 가 잡담을 분모에서 제외하면 실질 hit rate 가 더 정확.

### V3-PLAN §1.4 echo chamber 직접 탐지 (자기충족 메모리)

V3-PLAN §1.4 가 "MindVault v2 운영 중 (품질 양호)" 메모리가 형 실제 경험과 모순돼도 자동
교정 안 됨 위험 지적. Sprint 15 첫 실측에서 그 메모리 자체가 self-affirming top-1 으로
탐지됨 — "완성" + "운영 중" + "품질 양호" 5 hits. 형이 수동 검토 후 정정/삭제 대상.
나머지 후보들 (/assemble V4 Spike 시리즈) 도 ship-ready 단정형 본문 → 재검토 대상.

### false positive rate 0.0% 의 의미

39 표본 (다음 user turn 식별 가능한 recall) 중 negative cue 발화 0건. 형이 회수 결과에
명시적 불만 안 표시. 다만:
- 표본 작음 — 위양성 회수가 있어도 형이 명시적 부정 안 하면 metric 미감지
- 진짜 false positive ≠ 명시 negative cue. 다음 sprint 의 query intent classifier 가
  "추측 기반" 차단으로 보완.

## 안전 정책 준수

- `indexer.full_rebuild()` 호출 없음.
- DB 무관 (metrics.jsonl + projects/\*/\*.jsonl read-only).
- BGE plist + `bge_m3_server.py` 무변경.
- launchctl `com.yonghaekim.arctic-ko-mlx` 무관.
- production data read-only — 메모리 자산·인덱스 무변경.
- 자동 게이트 조정 미구현 — 형이 실측 metric 보고 수동 결정.

## 미해결 / Sprint 16+ 후보

- **부분 표본** false positive — 표본 39건 작음. negative cue 가 형 발화 안 잡으면
  underestimate. Sprint 16 query intent classifier 가 보완.
- **internal effort 0.60 의 분포** — 평균이 낮아도 long-tail (5+ tool_use 후속) 비율 측정 필요.
  현재는 per_event 만 sample 5건 노출. 분포 히스토그램은 다음 sprint.
- **scan_self_affirming_memories duplicates** — `-Users-yonghaekim/` vs
  `-Users-yonghaekim-my-folder/` 동명 메모리 양쪽 indexed → 같은 affirming pattern 이
  두 번 카운트. 실측에서 8건 중 일부는 중복 가능성. Sprint 11 미해결 dedup 작업 합치면 해결.
- **hours_back 의 양극화** — 24h 면 표본 작음, 168h+ 면 turn 합치는 시간 길어짐
  (실측 약 50s). Sprint 16 에서 jsonl 사전 정렬 / 인덱싱 캐시 검토.

## 변경 파일

```
src/self_eval.py                  | 신규 (320 lines)
tests/test_self_eval.py           | 신규 (380 lines, 19 tests)
handoff/SPRINT-15-BUILD-LOG.md    | 신규
```
