---
name: handoff-v3-plan
description: V3 master plan — v2.9 한계 5가지(procedural type 누락, false positive, internal effort, 자기수정 부재, wikilink 노이즈) + LLM-as-compiler 패러다임(Karpathy wiki) + Sprint 13~17 분해 + v3 target metric. Sprint 13~16 자율 실행 후 §5 표에 실측 컬럼 추가
---

# MindVault v3 PLAN — LLM-as-compiler + Procedural Memory

*Drafted: 2026-05-23, post v2.9 ship-ready (master HEAD `6cb6818`)*
*Sprint 11 결과는 끝난 후 §1 끝에 반영. Sprint 13~16 자율 실행 결과는 §5 metric 표에 반영됨 (master HEAD `44df753`).*

---

## 1. v2.9 한계 정리 (V3 진입 근거)

v2.9 = "v3 직전의 완성형 v2" — 한국어 도메인 분리 11배, hook 자동 회수, 동시성
안정성까지 완성. 그러나 실전 사용에서 **구조적 한계** 노출:

### 1.1 메모리 type 협소 (가장 결정적)

| Memory type | v2.9 저장 trigger | 저장됨? |
|---|---|---|
| **결정/learnings** (decisions) | Gemma extractor (`feedback-*`, `project-*`, `reference-*`) | ✓ 잘 됨 |
| **절차적 지식** (procedural — 명령어 syntax, workflow, 환경 설정) | **trigger 없음** | ✗ 누락 |
| **잠재적 패턴** (자주 반복하는 작업 흐름) | 없음 | ✗ |
| **부정적 사실** (안 되는 것, 함정) | feedback-* 일부 잡힘 | △ 일관성 부족 |

증거: 형이 `claude --bg` 명령어를 자주 쓰는데 그 syntax 메모리가 단 한 줄도
없음. 새 세션이 매번 `claude --help | grep` 부터 다시 시작.

### 1.2 회수 false positive (mid-cosine zone)

generic Korean query 가 도메인 게이트(0.40) 일부 통과 → 무관 메모리 회수.
예: 형의 메타 질문에 "MindVault v3 운영 중 (품질 양호)" 메모리가 자기충족
시그널로 회수. v2 가 자기 칭찬 메모리만 보여주는 echo chamber 위험.

### 1.3 Claude internal effort 가 metric 으로 안 보임

hook 이 picked > 0 줘도 Claude 가 한참 뒤지는 case 있음 (snippet 200자 cut 등 —
Sprint 11 에서 fix 진행 중). 회수가 "성공"인지 "충분"인지 측정 불가.

### 1.4 자기-수정 메커니즘 없음

"v2 품질 양호" 메모리가 형 실제 경험과 모순돼도 자동 교정 없음. 한 번 박힌
메모리가 정상이 아니면 영구적으로 잘못된 신호.

### 1.5 wikilink expansion 노이즈

target memory 의 query relevance 없이 끌려옴. **Sprint 11 B 에서 fix 완료** (`WIKILINK_GATE_FACTOR=0.75`).

### 1.6 Sprint 11 + 12 결과 — v2.9 → v2.9.2 보강 (2026-05-23)

V3-PLAN 작성 직후 별도 background 세션에서 v2.9 후속 fix 두 sprint 추가 완료.
master HEAD `35c33f3`.

**Sprint 11** (`854dfe1`) — 회수 품질 결함 3종 처리:

| 측정 | Sprint 10 baseline | Sprint 11 후 |
|---|---|---|
| picked 건수 (형 dbe query) | 2 (정답 1 + 무관 wikilink 1) | 1 (정답만) |
| 발췌 길이 | 159자 | 600자 (4x) |
| 발췌 도달 깊이 | repo 경로 + DB 위치 헤더 | Sprint 6 결정사항 (precision@3 23%→50% 등) 까지 |
| wikilink 노이즈 | feedback-transcript-lone-surrogate (raw 0.25, 무관) | 차단됨 (`gate=0.300 vs target=0.235`) |
| hook elapsed_ms | ~72 | 62~66 (cache hit) |

- A. `SNIPPET_CHARS` 160→600 + `_query_window` (query 매치 밀집도 ±half window) + `BROAD_WORD_FREQ_LIMIT=5` (memory 이름과 동일한 broad keyword 매치 후보 제외).
- B. `_expand_wikilinks` 에 `raw_cosine_map + raw_cosine_min` 전달, `WIKILINK_GATE_FACTOR=0.75` — target raw < `raw_cosine_min × 0.75` 차단. 동일 도메인 cluster wikilink 보존 확인.
- C. `MV3_EXTRA_MEMORY_DIRS=path1:path2` 환경변수 — handoff/ 등 추가 디렉토리 indexing scope 진입.

**Sprint 12** (`35c33f3`) — Sprint 11 후 추가 발견:

- handoff/ 8개 .md 에 YAML frontmatter 추가 → description vec rows **0 → 8**.
- 측정: `SPRINT-10-BRIEF` description rank 1 (raw 0.393) for query "dragonkue Arctic swap". `handoff-sprint11-build-log` rank 1 (raw 0.462) for 형 dbe query — 본문 발췌가 Sprint 11 측정 데이터 표(160→600 비교)까지 도달.
- **회귀 발견**: 새로 indexed 된 handoff 메모리 본문이 잡담 query 단어와 우연 FTS 매칭 → fts-only hit (raw 0.11) 으로 기존 FTS 면제 정책 통과. "안녕하세요 오늘 날씨" 같은 잡담에 `handoff-sprint11-build-log` 회수.
- **fix**: `fts_gate = raw_cosine_min × 0.5`. fts-only hit 도 `raw_cosine` 검사 (잡담 0.10 차단, 정확 keyword 매칭 0.20+ 통과). `raw_cosine_map` 비어있을 때 (vec 서버 다운) 면제 — FTS-only fallback 보존.

**의미**: 1.1~1.5 한계 중 1.3 (Claude internal effort), 1.5 (wikilink 노이즈) 가 v2.9.2 에서 본질적 개선. **남은 v2.9 한계는 1.1 (procedural type 누락), 1.2 (false positive — query intent 차원), 1.4 (자기-수정 메커니즘 없음)** — 이게 V3 진입 정당화.

---

## 2. v3 패러다임 (LLM as compiler)

Karpathy LLM Wiki 패턴 채택 — 매 세션이 RAG 로 매번 검색하는 게 아니라,
**LLM 이 컴파일러처럼 raw 세션 → 정제된 wiki 항목**을 누적 생성·업데이트.

**v2.9** = 사람이 결정 메모리 작성 → Gemma 후보 추출 → 사람 승인
**v3.0** = LLM 이 세션 끝에 자동으로 raw → wiki 변환, 사람은 검토만 (또는 skip)

핵심 차이:
- 메모리는 **점진 정제**되는 살아있는 문서 (immutable list 가 아님)
- 새 정보 들어오면 기존 항목 **update vs 신규** 자동 판단
- 절차적 지식까지 포함하는 wider net

---

## 3. v3 핵심 기능

### A. Procedural Memory Slot (Sprint 12)

**목표**: workflow·명령어·syntax·환경 설정 자동 저장.

**구현**:
- `memory/_procedural/` 디렉토리 별도 슬롯 (기존 `memory/` 와 분리)
- frontmatter `type: procedural` 또는 `kind: workflow` 등 새 메타
- SessionEnd hook 확장: "이 세션에서 사용된 새 명령어·flag·syntax" Gemma 추출
- trigger 패턴: `claude --bg`, `git worktree add`, `launchctl load`, `sed -i ''` 같이
  검색·실험·발견한 흔적
- 형 본인 명시 trigger 없이 자동 (사용자 행동 제로 원칙)

### B. Memory Compiler (Sprint 13)

**목표**: LLM 이 세션 raw → 정제된 wiki 항목 생성·업데이트.

**구현**:
- SessionEnd 에서 `claude -p --bare` 로 raw transcript → 구조화 wiki 항목.
- 기존 메모리 path 매칭 → update vs 신규 자동 판단.
- update 시 outdated 부분 제거 + 새 사실 통합 (Karpathy wiki 의 핵심).
- 검토 UI: `/memory_review` 확장 — diff 보기, 자동 승인 옵션.

**기술 요소**:
- Gemma 4 E4B 가 정제 LLM (로컬, 비용 0).
- 또는 옵션: Claude Sonnet 4.6 (cloud, 더 좋은 정제 품질 — Sprint 13 비교).

### C. Self-evaluation Loop (Sprint 14)

**목표**: 회수 품질 자동 측정 + 자가 교정.

**구현**:
- **Internal effort metric**: Claude 가 hook 회수 받은 후 추가 read·grep·ls 한
  횟수 카운트. JSONL 분석 (tool_use 이벤트). 회수가 충분했으면 0, 부족하면 N.
- **False positive 추적**: hook 회수 → 형이 다음 turn 에 "무관" 키워드 (예: "이거
  관계없는데", "왜 X 가 왔지") 응답 패턴 자동 감지 → 해당 회수 penalty.
- **자동 게이트 조정**: false positive 비율 임계 초과 시 raw_cosine_min 0.40 →
  0.45 자동 상향 (또는 query intent classifier 가 처리).
- **자기충족 메모리 감지**: "X 가 잘 작동 중" 류 self-affirming memory 가
  형의 실제 사용 패턴 (오류·재시도·검색)과 모순할 때 flag.

### D. Query Intent Classifier (Sprint 15)

**목표**: "회수 필요" vs "잡담·메타" 자동 분류.

**구현**:
- 로컬 분류기 (Gemma small / 또는 fastText 한국어).
- input: 형 query. output: `{recall_intent, code_intent, meta_intent, chat_intent}`.
- meta/chat 일 때 hook 회수 0건 강제 (raw 0.5 통과해도 차단).
- 본질적으로 mid-cosine zone discriminator. 게이트 단순 숫자보다 정확.

### E. Multi-source Indexing (Sprint 15+)

**목표**: memory/*.md 외 자료까지 hook 회수 scope.

**대상**:
- repo 내 `handoff/*.md` (Sprint 11 후보 C)
- 형의 다른 active repo 의 README, CLAUDE.md
- 옵션: 블로그 (vibe1977.tistory.com), 유튜브 자막

**기술**: 환경변수 `MV3_EXTRA_MEMORY_DIRS` 또는 explicit registry.

### F. Multi-modal (Optional, v3.5+)

- 코드 + 로그 + 이미지 메모리 (현재는 *.md only).
- 우선순위 낮음 — v3.0 ship 후 사용자 요구 보고 결정.

---

## 4. 마이그레이션 경로 (v2.9 → v3.0)

**점진 호환** 채택:
- 기존 `memory/*.md` 그대로 호환 (frontmatter type 미지정 = "decision" 디폴트).
- 새 procedural slot 은 `memory/_procedural/` 별도 디렉토리, 점진 추가.
- hook 호출 시 두 slot 모두 검색 (별도 게이트).
- Sprint 13 Memory Compiler 는 opt-in (`MV3_AUTO_COMPILE=1`) 으로 시작 →
  안정화 후 default on.

**클린 컷 안 함** — 형의 80+ 메모리 자산 보존이 v2.9 가치 핵심.

---

## 5. 검증 Metric

v2.9 베이스라인 + v3 target + Sprint 13~16 자율 실행 후 실측 (Sprint 15 self_eval 측정).

| Metric | v2.9 baseline | v3 target | v3 실측 (master HEAD `44df753`) |
|---|---|---|---|
| hook hit rate | ~79% (도메인 query 추정) | 90%+ | **66.5%** (전 호출 168h, 292/439). Sprint 16 classifier 가 잡담·메타 분모 제외 시 실질 hit 상승 예상 |
| false positive rate | 미측정 | <5% | **0.0%** ✓ (표본 39건, Sprint 15 negative cue 미발화) |
| Claude internal effort (avg follow-up read·grep) | 미측정 | <1 | **0.60** ✓ (recall 후 ~ 다음 user turn 사이 avg tool_use) |
| procedural memory coverage | 0% (slot 없음) | 70%+ 형 자주 쓰는 명령어 | **0%** baseline (695 bash / 42 binary, `--procedural-audit --hours 720`) — Sprint 13 인프라 + NEXT-1 자동 trigger 휴리스틱(special_binary OR non_trivial + NEXT_ACTION) 완성. 운영 누적 후 재측 예정 |
| 자기 모순 메모리 감지율 | 0% | 80%+ | **8건 후보 탐지** (`scan_self_affirming_memories`) — §1.4 echo chamber 의 "MindVault v1 폐기 / v2 운영" 메모리 직접 발견 |
| session-end auto compile latency | N/A | <10s (Gemma local) | 미측정 (`MV3_AUTO_COMPILE` opt-in 단계, 운영 fire 0건) |
| duplicate memory | 미측정 | 0 | **name-dup 0, stem-collision 1** (project_mindvault) — `dedup_cli.py` 인프라 완성, stem 충돌 1건은 형 검토 영역. NEXT-2 embedding fallback (`_find_by_embedding`, cosine ≥ 0.75) 추가로 자연어 변형도 같은 메모리로 update 수렴 가능 — 신규 staged 쌓임 ↓ 예상 |
| test isolation | 5 fail (master 35c33f3) | 0 fail | **0 fail** ✓ (`63f32df` 에서 pre-existing 결함 5건 모두 해소) |

---

## 6. Sprint 분해

| Sprint | 주제 | 산출물 |
|---|---|---|
| 13 | Procedural Memory Slot | `memory/_procedural/`, SessionEnd extract trigger 확장 (workflow·명령어·syntax) |
| 14 | Memory Compiler | session → wiki 자동 정제, diff review UI, update vs 신규 자동 판단 |
| 15 | Self-eval Loop | internal effort metric, false positive 추적, 자동 게이트, 자기충족 메모리 감지 |
| 16 | Query Intent Classifier + Multi-source | mid-cosine zone discriminator, 외부 repo 인덱싱 |
| 17 (ship) | v3.0 안정화 + GitHub 배포 | uninstall.sh + README v3 + Karpathy wiki blog post |

각 sprint 2-3일 작업, 총 ~3주.

**Sprint 번호 shift 사유**: Sprint 11(`854dfe1`) + Sprint 12(`35c33f3`) 가 v3 가 아닌 **v2.9.1/v2.9.2 후속 fix** 로 master 에 들어감 (회수 quality + handoff frontmatter + FTS 회귀 fix). V3 첫 sprint 는 Sprint 13 부터.

§5 의 multi-source indexing 일부 (handoff/) 는 Sprint 11 C 에서 이미 처리됨 — Sprint 16 multi-source 는 외부 repo + 옵션 자료 (블로그·자막) 위주로 좁아짐.

---

## 7. 위험·완화

| 위험 | 완화 |
|---|---|
| Memory Compiler 가 좋은 정보 삭제 | diff review + 1회/일 백업 (git commit) |
| Procedural slot 이 노이즈로 가득 | Gemma trigger 정밀화 + 사용자 승인 default-on |
| Self-eval Loop 가 잘못 학습 → 게이트 망가짐 | metric 기반 + manual override 가능 |
| Query Intent Classifier 정확도 부족 | dual-track (classifier + raw_cosine), classifier confidence 낮을 때 raw 사용 |
| v2.9 회귀 | 기존 layer 1-4 보존, v3 기능은 opt-in 으로 시작 |

---

## 8. 결론

v2.9 = "decisions·learnings 자동 회수" 까지는 완성.
v3.0 = "**procedural + self-evolving wiki**" 추가 → 진짜 의미의 세션 간 기억.

핵심 차이: v2.9 는 "사람이 기억할 만한 것" 을 자동 회수. v3.0 은 "LLM 이 자기
기억을 컴파일" 하는 시스템.

v3 첫 ship 목표: Sprint 12 시작 ~ Sprint 16 ship, 3주 이내.
