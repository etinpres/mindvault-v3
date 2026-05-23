---
name: handoff-v3-next-improvements
description: V3 차기 보강 7개 micro-sprint 후보 brief — 자동 trigger 휴리스틱·embedding 매칭·Gemma 보강 classifier·type 별 게이트·diff 색상·slug conflict·scan latency 캐시. 새 세션에서 형이 선택한 1개 해결하는 self-contained 컨텍스트
---

# MindVault V3 — 차기 보강 micro-sprint brief

*Drafted: 2026-05-23, master HEAD `97ac2ee` (Sprint 13~16 + dedup + V3 docs + metric 보강 + Compiler ON 후 상태).*

새 세션에서 **아래 7개 미완 항목 중 형이 지정한 1개**를 해결한다.
각 항목은 micro-sprint (반나절~1일) 규모. 자율 진행 권한 동일.

## 시작 시 확인

- 형이 prompt 에 "X 항목 진행" 또는 "#3 진행" 식으로 명시했으면 즉시 그 항목 시작.
- 명시 안 했으면 7개 목록 보여주고 형 선택 받기 (`AskUserQuestion` 1회).
- 작업 시작 전 반드시 `/goal` 으로 종료 조건 설정 권장:
  - `master 에 commit 완료 + handoff/SPRINT-XX-BUILD-LOG.md 작성 + production sync + 회귀 PASS`

## 환경 (현재 상태)

- 작업 dir: `/Users/yonghaekim/my-folder/apps/mindvault-v2`
- master HEAD: `97ac2ee feat(compiler_benchmark): Memory Compiler latency benchmark`
- 운영 임베딩: Arctic-ko MLX 4bit, port 8081
- DB: `~/.claude/mindvault-v2/index.db` (WAL)
- Hook: Sprint 13~16 + intent classifier + sources.json multi-source ON
- 운영 sources.json: `handoff/` + auto-memory dir 영구 등록
- **Memory Compiler ON 상태** (`MV2_AUTO_COMPILE=1`, LaunchAgent `com.yonghaekim.mv2-env.plist` 영구화 완료)
- 한국어 응답, 토큰 절약 룰 (CLAUDE.md 참조)
- Three Man Team 사용 가능 (Arch / Bob / Richard)

## 작업 범위 — 7개 미완 항목

각 항목은 BUILD-LOG·V3-PLAN 의 한계 정리에서 파생. 본 brief 안에 self-contained 컨텍스트 포함.

### #1. 자동 trigger 휴리스틱 (Sprint 13 미완)

**현 상태**: `memory_extractor.TRIGGER_RE` 가 명시 키워드 (`기억해`, `결정:`, `외워둬`, `이 명령어` 등) 만 매칭. 사용자가 명시 발화 안 하면 candidate 추출 안 됨.

**해결 방향**:
- 명령어·workflow·syntax 발견 흔적 자동 감지 — assistant 의 Bash tool_use 안에 새로운 명령어·flag 패턴이 등장하면 trigger.
- 또는 형 자주 쓰는 binary (`procedural_audit` top-20) 의 새 옵션 등장 → 자동 후보.
- 단 false trigger 폭증 위험 — 기존 trigger 와 OR 결합, gemma extractor 가 최종 판별.

**참고 자료**: `~/.claude/scripts/mindvault/self_eval.py --procedural-audit --hours 720`

---

### #2. embedding 기반 의미 매칭 (Sprint 14 미완)

**현 상태**: `memory_compiler._find_existing_memory` 가 (1) frontmatter `name` exact 매칭 → (2) slugify(title) fallback. 같은 주제를 다른 표현 변형 (예: "claude --bg" vs "백그라운드 세션") 으로 가리키면 매칭 못 함.

**해결 방향**:
- candidate body 의 임베딩 → 기존 memories_vec 와 cosine top-1
- threshold (예: 0.75+) 통과한 path 만 update 후보로
- name/slug 매칭 우선, 안 되면 embedding fallback (3순위)
- 잘못된 매칭으로 wrong memory overwrite 위험 → diff review 단계 의존

**참고**: V3-PLAN §3.B / SPRINT-14-BUILD-LOG "미해결 1번"

---

### #3. Gemma 보강 classifier (Sprint 16 미완)

**현 상태**: `query_intent.classify` rule-based regex 만. unknown intent 영역에서 borderline 잡담·메타 가 그대로 통과해 hook 호출됨.

**해결 방향**:
- intent=unknown + 짧은 query (예: <20자) 일 때만 Gemma 호출 보강
- Gemma 가 chat/meta/code/recall 분류 → confidence 높으면 hook recall skip
- latency 추가 ~ Gemma 100~300ms (4B MLX) → 짧은 query 한정이라 매 hook 호출 영향 없음
- hook 단의 graceful timeout — Gemma 미응답 시 fallback to rule-based

**참고**: V3-PLAN §3.D / SPRINT-16-BUILD-LOG "미해결 2번"

---

### #4. type 별 회수 게이트 분리 (Sprint 13 미완)

**현 상태**: procedural/feedback/project/reference 메모리 모두 동일 raw_cosine_min 게이트 (DEFAULT 0.40, HINTED 0.32). procedural 은 specific keyword 매칭이 더 강해야 정확하지만 일반 결정 메모리와 동일 임계.

**해결 방향**:
- `recall_memory` 가 path 의 type (frontmatter 또는 `_procedural/` slot) 보고 게이트 분기
- procedural: 더 엄격 (예: 0.45) — specific keyword 매칭만 통과
- feedback/project: 기존 0.40 유지
- Sprint 15 self_eval intent_stats 가 type 별 hit rate 측정해 튜닝 결정

**참고**: SPRINT-13-BUILD-LOG "미해결 2번"

---

### #5. diff UI 색상 highlight (Sprint 14 미완)

**현 상태**: `memory_review_cli.py diff` 가 JSON 안에 raw unified_diff 문자열. CLI 호출 시 그냥 텍스트.

**해결 방향**:
- `--pretty` 옵션 추가 — `+` 라인 green, `-` red ANSI 색상
- 또는 별도 sub `pretty-diff <file>` — JSON 안 거치고 직접 색상 텍스트 출력
- 형이 매 update approve 전에 diff 빠르게 훑게 → 정제 품질 검토 비용 ↓

**참고**: SPRINT-14-BUILD-LOG "미해결 3번"

---

### #6. session 내 동일 slug conflict (Sprint 14 미완)

**현 상태**: 같은 SessionEnd 안에서 동일 slug candidate 가 두 개 이상 추출되면 `session_memory_end.existing_slugs` dedup 로직이 마지막 하나만 살리고 나머지 skip. compiler 가 같은 기존 path 와 매칭되는 케이스에서 잠재적 정보 손실.

**해결 방향**:
- 동일 slug candidate 들을 1건으로 미리 merge (body 합치기 또는 Gemma compile 누적)
- 또는 staged 파일명에 timestamp 외 suffix (`_2`, `_3`) 부여
- frequency 측정: debug.log 의 `dup slug` skip 로그 카운트해 영향 빈도 확인

**참고**: SPRINT-14-BUILD-LOG "미해결 4번"

---

### #7. self_eval scan latency (Sprint 15 미완)

**현 상태**: `analyze_recent --hours 168` 실측 ~50초. 모든 jsonl turn 메모리 로드. `--hours 720` (30일) 면 더 길어짐 — Bash CLI 호출이 답답함.

**해결 방향**:
- jsonl 인덱싱 캐시: `~/.claude/mindvault-v2/turns_cache.db` 같은 sqlite 에 (ts, role, text, tool_uses) 사전 정리
- analyze 시점에 캐시만 query → 50s → <5s 예상
- mtime watch 로 새 jsonl 자동 incremental update
- `--rebuild-cache` 플래그 별도

**참고**: SPRINT-15-BUILD-LOG "미해결 4번" + 신규 scan_self_affirming_memories 도 같이 캐시 활용 가능

---

## 자율 결정 권한 + 안전 원칙

**결정 권한** (사용자 confirm 없이 진행):
- 디자인 결정 (스키마·threshold·plist label 등) — BUILD-LOG 에 사유 명시
- 실패 시 sprint 단위 rollback
- false positive 발견 시 즉시 fix (Sprint 12·Sprint 15 #1 패턴)

**안전 원칙** (위반 금지):
- `indexer.full_rebuild()` 호출 금지
- Sprint 10 트랜잭션 패턴 (매 iter conn.commit + embed_text reordering) 유지
- BGE plist + `bge_m3_server.py` 보존 (롤백 경로)
- 운영 launchctl 서비스 (`com.yonghaekim.arctic-ko-mlx`, `com.yonghaekim.gemma-mlx`, `com.yonghaekim.mv2-env`) 건드리지 말 것
- 작업 시작 시 **EnterWorktree 격리** (예: `next-N-<항목명>`)
- 변경 전 production 위치 (`~/.claude/scripts/mindvault/` + `~/.claude/hooks/`) 백업
- 회귀 검증 필수 (잡담 차단, 도메인 hit, 동시성 lock 0건)
- Sprint 17 (ship) 관련 작업 자동 제안 금지 — `feedback-ship-defer` 메모리 참조

## 진행 순서 권장

1. **Setup**: V3-PLAN.md + 해당 항목의 SPRINT-BUILD-LOG 정독. master HEAD ack.
2. **/goal** 호출 (종료 조건 명시).
3. **EnterWorktree** → 작업 → 회귀 → commit → master ff merge → production sync.
4. **BUILD-LOG**: `handoff/SPRINT-NEXT-<항목>-BUILD-LOG.md` 또는 형 선호 네이밍.
5. **iCloud 갱신**: 본 brief 가 iCloud 에 있으므로 작업 완료 후 master HEAD 업데이트 한 줄 추가도 OK (선택).

## 메모리 (형 의도)

- **`feedback-ship-defer`**: ship 작업 (README v3, install/uninstall 안내, GitHub MIT 공개) 은 형이 별도 지시할 때까지 보류. 결과 보고에 ship 진행 권유 prompting 금지.
- **`no-v1-token-waste`**: 회수·메모리 시스템 변경 시 매 메시지마다 무관 메모리 주입 안 되게 신중히. self_eval false positive 측정 후 결정.
- **`reference-llm-wiki-pattern`**: Karpathy LLM Wiki 가 v3 이론 토대. #2 embedding 매칭이 본 패턴의 자연스러운 다음 단계.

## 추가 검토 자료

- `handoff/V3-PLAN.md` — V3 master plan + §5 metric 실측 표
- `handoff/V3-MASTER-BRIEF.md` — Sprint 13~16 자율 작업 brief (참고용)
- `handoff/SPRINT-13~16-BUILD-LOG.md` — 각 sprint 결정·측정 데이터
- `~/.claude/scripts/mindvault/self_eval.py --hours 168` — 최신 운영 metric (hit rate, internal effort, false positive, intent 분포)
- `~/.claude/scripts/mindvault/dedup_cli.py list` — duplicate memory 현황
- `~/.claude/scripts/mindvault/compiler_benchmark.py --repeats 3` — Memory Compiler latency

각 항목 작업 끝나면 V3-PLAN.md §5 표 또는 별도 metric 표에 보강 실측 한 줄 추가 권장.
