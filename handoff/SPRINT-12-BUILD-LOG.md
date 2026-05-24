---
name: handoff-sprint12-build-log
description: Sprint 12 build log — handoff 8개 .md 파일에 YAML frontmatter(name+description) 추가해 description 임베딩 활성화 + FTS-only fallback 잡담 회귀 fix(vec_available 조건 추가, fts_gate=raw_cosine_min×0.5)
---

MindVault v3 Sprint 12 — handoff frontmatter + FTS 게이트 강화 빌드 로그

## 요약

Sprint 11 에서 handoff/ indexing scope 활성화는 했지만 frontmatter 부재로 description 임베딩 생성 안 됨 → cosine 분포 낮음 (0.39~0.41) → top-K=1 정책상 picked 안 됨. Sprint 12 에서 handoff/ 8개 .md (V3-PLAN.md 는 untracked 라 제외) 에 frontmatter 추가. 측정 중 FTS-only hit 회귀 (잡담 query 가 단어 우연 매칭으로 통과) 발견 → memory_search.py 의 FTS 면제 정책 강화.

## 변경 상세

### A. handoff/ frontmatter (8개 파일)

각 파일 head 에 YAML frontmatter 삽입:

```yaml
---
name: <kebab-case-slug>
description: <Sprint/주제 + 핵심 결정·키워드>
---
```

| 파일 | name |
|---|---|
| ARCHITECT-BRIEF.md | handoff-architect-brief-sprint3 |
| BUILD-LOG.md | handoff-build-log |
| REVIEW-FEEDBACK.md | handoff-review-feedback-sprint3 |
| REVIEW-REQUEST.md | handoff-review-request-sprint3 |
| SESSION-CHECKPOINT.md | handoff-session-checkpoint-sprint4 |
| SPRINT-10-BRIEF.md | handoff-sprint10-brief |
| SPRINT-10-BUILD-LOG.md | handoff-sprint10-build-log |
| SPRINT-11-BUILD-LOG.md | handoff-sprint11-build-log |

description 은 query 매칭 잘 되도록 keyword 풍부하게 작성 (예: Sprint 10 brief 는 `incremental_index transaction commit hook sqlite lock dragonkue Arctic-Embed-L`).

V3-PLAN.md 는 본 repo master untracked — 형 의도적 제외라 Sprint 12 scope 밖.

### B. FTS-only fallback 회귀 fix (`src/memory_search.py`)

기존 게이트:
```python
if raw_cosine_min > 0 and raw < raw_cosine_min and "fts" not in info["source"]:
    continue  # fts source는 면제
```

Sprint 12 에 새로 indexed 된 SPRINT-11-BUILD-LOG.md 의 본문 어떤 단어가 잡담 query 와 FTS BM25 매칭 → fts-only hit (raw 0.11) 으로 차단 우회 → 잡담에 회수 발생. **회귀 발견**.

수정:
```python
vec_available = bool(raw_cosine_map)
fts_gate = raw_cosine_min * 0.5  # fts-only 면제 완화 (raw 0.20 minimum)
for path, info in combined.items():
    raw = raw_cosine_map.get(path, 0.0)
    if raw_cosine_min > 0 and vec_available:
        has_vec = "vec" in info["source"]
        threshold = raw_cosine_min if has_vec else fts_gate
        if raw < threshold:
            continue
    kept.append(...)
```

- vec 임베딩 자체가 동작 안 함 (서버 다운 등 — `raw_cosine_map` 비어있음) 면 게이트 면제 → FTS-only fallback 유지.
- vec 작동 중인데 fts-only hit 이면 `raw_cosine_min × 0.5` 임계. 잡담 (raw 0.10 안팎) 차단, 정확 keyword 매칭 (raw 0.20+) 통과.
- vec hit 인 path 는 기존 `raw_cosine_min` 그대로.

## 측정 데이터

### handoff/ vec rows (전후)

| 측정 | Sprint 11 | Sprint 12 |
|---|---|---|
| handoff/ memories rows | 8 (env + 수동 indexer 후) | 9 (V3-PLAN 포함, untracked) |
| handoff/ description vec rows | 0 (frontmatter 부재) | 8 (V3-PLAN 빼고 모두) |
| handoff/ body vec rows | 8 | 9 |

### 형 query 재현 (`mindvault dbe 모델 말고 이름이 기억 안나는데 다른 모델로 바꿔서 그 성능이 얼마나 좋아 졌었지?`)

| 항목 | Sprint 11 | Sprint 12 |
|---|---|---|
| top-1 picked | project-mindvault (raw 0.450) | **handoff-sprint11-build-log (raw 0.462)** |
| handoff 후보 best rank | rank 5 (V3-PLAN, raw 0.405) | rank 1 (SPRINT-11-BUILD-LOG) |
| BUILD-LOG description rank | (vec row 부재) | rank 10 (raw 0.396) |
| 발췌 내용 | Sprint 6 결정사항 | Sprint 11 측정 데이터 표 (160→600 비교) |

### Specific query (`dragonkue Arctic 모델 swap cosine 측정`)

| 항목 | Sprint 11 | Sprint 12 |
|---|---|---|
| top-1 | (게이트 0.40 차단, no hits) | SPRINT-10-BRIEF description (raw 0.393) — gate 0.40 직전, hinted 0.32 적용 시 hit |
| top-3 vec hit | — | SPRINT-10-BRIEF desc → SPRINT-10-BRIEF body → SPRINT-11-BUILD-LOG desc |

### 회귀 케이스

| query | gate | 결과 | 의도 |
|---|---|---|---|
| `안녕하세요 오늘 날씨` | 0.40 | (no hits) | 잡담 차단 ✓ Sprint 12 fix 적용 |
| `grammar saas 영어 선생님 반복 학습` | 0.32 | project-grammar-saas + 2 wikilink-1hop | wikilink cluster 보존 ✓ |
| `youtube 영상 만들어줘` | 0.40 | 유튜브 롱폼 파이프라인 (raw 0.457) | 다른 도메인 정상 ✓ |
| `mindvault sprint 진행 상황` | 0.40 | project-mindvault (raw 0.627) | 도메인 hit 정상 ✓ |

### 회귀 테스트

- `tests/test_memory_search.py`: 11/11 PASS (FTS-only fallback 케이스 정상 통과)
- `tests/test_memory_hook.py`: 5/5 PASS
- 형 query end-to-end: SPRINT-11-BUILD-LOG top-1 (score 0.73 normalize, vec+fts)

## 미해결 / Sprint 13 후보

1. **V3-PLAN.md 처리** — 본 repo master untracked. 형 의도적 제외인지, 미완성 작업물인지 확인 필요. 의도적이라면 `.gitignore` 추가, 회수 대상이라면 frontmatter + git add.
2. **duplicate memory dedup** — Sprint 11 미해결, `-Users-yonghaekim/` vs `-Users-yonghaekim-my-folder/` 동명 메모리.
3. **install.sh 에 `MV3_EXTRA_MEMORY_DIRS` 안내** — 현재 형 shell rc 수동 export 필요 (이번 Sprint 11 활성화로 완료).
4. **embed_cache test isolation** — `test_embed_timeout_returns_none` master HEAD 부터 pre-existing fail.

## 변경 파일

```
handoff/ARCHITECT-BRIEF.md       | +5 (frontmatter)
handoff/BUILD-LOG.md             | +5
handoff/REVIEW-FEEDBACK.md       | +5
handoff/REVIEW-REQUEST.md        | +5
handoff/SESSION-CHECKPOINT.md    | +5
handoff/SPRINT-10-BRIEF.md       | +5
handoff/SPRINT-10-BUILD-LOG.md   | +5
handoff/SPRINT-11-BUILD-LOG.md   | +5
src/memory_search.py             | +9 -3 (FTS 게이트 강화)
handoff/SPRINT-12-BUILD-LOG.md   | 신규
```
