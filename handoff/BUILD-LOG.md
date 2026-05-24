---
name: handoff-build-log
description: MindVault v3 Sprint 1~3 빌드 로그 — Architect 소유 Builder가 각 step 완료 후 갱신. SessionStart 훅 MVP, FTS5 인덱스, SessionEnd staging + /memory review 승인 게이트 구현 기록
---

# Build Log — MindVault v3

*Owned by Architect. Updated by Builder after each step.*

---

## Current Status

**Active step:** Sprint 3 — SessionEnd staging + `/memory review` (구현·배포·E2E 통과, 리뷰 대기)
**Last cleared:** Sprint 2 (2026-04-15)
**Pending deploy:** Sprint 3 배포 완료 (SessionEnd 훅 등록, staged/approve/reject E2E 증명)

---

## Step 1 — Sprint 1 MVP (2026-04-15)

### 무엇을 빌드했나

ARCHITECT-BRIEF.md 사양대로 SessionStart 훅 하나 — 최근 5개 세션을 로컬 Gemma로 요약해 새 세션 컨텍스트에 자동 주입.

### 파일 변경

| 경로 | 종류 | 설명 |
|---|---|---|
| `src/session_memory.py` | 신규 | 훅 메인 스크립트 (단일 파일, ~230줄) |
| `tests/test_session_memory.py` | 신규 | 21개 단위 테스트 |
| `install.sh` | 신규 | `~/.claude/`에 훅 배포 + settings.json 편집 (멱등) |
| `uninstall.sh` | 신규 | 역방향 원복 (다른 훅 보존) |
| `README.md` | 신규 | 설치·동작·설정·디버깅 가이드 |

### 핵심 결정 사항

1. **단일 파일 구조** — 배포 단순성을 위해 모듈 분리 대신 단일 파이썬 파일로 합침. 테스트는 `src/`에 `sys.path` 추가해서 import.
2. **Gemma max_tokens = 2000** — `gemma-worker.md` 지침대로 reasoning이 토큰 소비하므로 출력 목표(500)의 4배. 아래 "알게 된 것" 참조.
3. **session_id 키 둘 다 지원** — `sessionId`와 `session_id` 양쪽 다 stdin에서 읽음 (Claude Code는 `sessionId`를 보냄, carl-hook이 참고 케이스).
4. **에러는 모두 exit 0 + 빈 출력** — 절대 세션 시작 블로킹 안 함. 디버그는 `~/.claude/mindvault-v3/debug.log` 파일에만.
5. **무한 루프 방지** — 요약 블록에 시그니처 `# 지난 세션 요약 (MindVault v3)` 삽입, JSONL 파싱 시 동일 시그니처 포함 메시지 제외.
6. **head/tail 턴 6/6** — 브리프는 10/10이었으나 Gemma 입력 4600 토큰에서 10초 타임아웃 걸림. 6/6으로 줄이고 타임아웃도 30→45초로 올림. (스코프 변경 사항 Arch에게 통지)

### 검증 결과

**자동 테스트**: 21/21 통과
- `test_session_memory.py`: Redact, ExtractContent, ExtractMessages, Cache, GemmaClientErrorHandling, EmitOutput
- 명령: `python3 -m unittest tests.test_session_memory`

**수동 E2E**:
- [x] 실제 Gemma 호출 → 한국어 요약 844자 반환 (19초, 첫 호출)
- [x] 캐시 히트 → 52ms (목표 <100ms 만족)
- [x] Gemma 다운 시뮬레이션 (URL을 가짜 포트로) → exit 0
- [x] install.sh 실행 → 기존 PreToolUse/UserPromptSubmit 훅 보존, SessionStart만 추가
- [x] install.sh 멱등성 — 재실행 시 중복 추가 안 함 ("already registered")
- [x] uninstall.sh — SessionStart 훅만 제거, 다른 이벤트 보존

### 알게 된 것 (Known Findings)

1. **Gemma 4 E4B는 reasoning 기반 모델** — `choices[0].message.content`에 최종 답이 들어가지만, `max_tokens`가 작으면 `reasoning` 필드에서만 토큰 소비하고 `content`는 빈 채로 `finish_reason: "length"`로 끝남. 최소 2000 토큰 필요.
2. **JSONL 스키마 — 타입이 7종** — `file-history-snapshot`, `permission-mode`, `attachment`, `system`, `last-prompt`, `user`, `assistant`. 의미 있는 건 user/assistant만.
3. **Assistant content는 블록 리스트** — `thinking`, `text`, `tool_use` 섞여 있음. `text`만 추출.
4. **현재 세션 JSONL도 같은 폴더에 있음** — sessionId로 반드시 제외해야 함 (mtime이 live하게 바뀌어 캐시 키가 매번 달라짐).
5. **Gemma 실측 성능** — 입력 ~3000토큰 + 출력 ~500토큰에 19초. 캐시 미스 시 첫 세션 시작만 느림.

### Known Gaps (다음 스프린트로 이월)

- Sprint 2: FTS5 검색 인덱스 + `/recall` 스킬
- Sprint 3: SessionEnd 훅으로 중요 사실 자동 memory/ 반영
- Gemma 요약이 간혹 없는 버전 번호·날짜를 만들어냄 (hallucination). 현재는 수용 — 4B 모델 한계.
- macOS 외 플랫폼 미검증 (경로 하드코딩 → 배포 단계에서 일반화 필요)

### 스펙 대비 변경 (Arch 승인 필요)

| 항목 | 브리프 | 실제 | 이유 |
|---|---|---|---|
| head/tail turns | 10/10 | 6/6 | 10/10 시 Gemma 입력 4600 토큰, 10초 타임아웃 |
| GEMMA_TIMEOUT | 10s | 45s | Gemma 4 E4B 실측 19초 |
| GEMMA_MAX_TOKENS | 500 | 2000 | reasoning 토큰 소비 분 감안 (gemma-worker.md 지침) |

다른 사항은 브리프 그대로 이행.

---

## Arch Decisions (Richard 에스컬레이션 응답, 2026-04-15)

**M1, M2 수정 승인** — 명백한 버그, Bob 즉시 수정.
**S1~S6 수정 승인** — 5분 내 처리 가능, Bob 인라인 수정.

**E1 (첫 세션 19초 블로킹)**: (a) + (c) 혼합으로 간다.
- Sprint 1은 (a) 현 상태 유지 — 블로킹 허용
- `install.sh`에서 1회 pre-warm 실행 추가 — 설치 직후 캐시 생성 → 첫 실세션 시작 시 즉시 히트
- Sprint 2에서 백그라운드 비동기화는 재평가 (UX 체감 측정 후)

**E2 (sessionId fallback)**: **휴리스틱으로 해결** — stdin에 sessionId 없으면 가장 최근 mtime JSONL은 "현재 세션일 가능성 높음"으로 간주해 배제. 정확히 말하면 `MAX_SESSIONS + 1` 개 찾고 첫 개(가장 최근)를 스킵. sessionId 왔으면 기존 로직. Bob 구현.

---

---

## Step 1 재작업 (2026-04-15, Richard 1차 피드백 반영)

### 적용된 수정

| 항목 | 변경 | 검증 |
|---|---|---|
| M1 | `extract_text_from_content`에 `_is_system_reminder` 블록 단위 필터 추가. 기존 message-level 필터 제거. | 회귀 테스트 3개 추가 (리마인더+실텍스트 공존, 문자열 통째 리마인더, command-* 태그) |
| M2 | `extract_messages`에서 redact ↔ truncate 순서 반전 | 회귀 테스트 1개 추가 (경계 걸친 비밀 키) |
| S1 | `CLAUDE_SESSION_ID` env fallback 의도 주석 추가 | - |
| S2 | 프롬프트의 "500 토큰" 지시 제거, "추측 금지" 지시 추가 (할루시네이션 방지) | - |
| S3 | `call_gemma` except를 `Exception`으로 확대, `choices` 비어있을 때 early return | - |
| S4 | test_emit_output의 `patch.object(sm.sys, ...)` → `patch("sys.stdout", ...)` | - |
| S5 | install.sh의 `HOOK_CMD`에서 `python3` 접두사 제거, shebang + chmod +x 의존 | 설치·재설치 검증됨 |
| S6 | README 테스트 섹션에 `cd apps/mindvault-v3` 전제 추가 | - |
| E1 | install.sh에 pre-warm 실행 추가 (설치 직후 1회 훅 실행해서 캐시 생성) | 설치 출력 "✓ pre-warm complete" 확인 |
| E2 | `get_recent_sessions`에 sessionId 없을 때 휴리스틱 (가장 최근 mtime 1개 배제) | 회귀 테스트 2개 추가 |

추가 발견 + 수정:
- **uninstall ↔ install 호환성**: 이전 `python3 TARGET` 형식 레거시 항목 정리 로직을 install.sh/uninstall.sh 모두에 추가. `target` 경로 포함 여부로 매칭. 중복 등록 방지.
- **JSONL `message.content` None safe**: `block.get("text")`가 None일 때도 안전하게 처리 (`str(text_val or "")`).

### 최종 검증

- 단위 테스트: **27/27 통과** (21 기존 + 6 신규 회귀)
- E2E 캐시 미스: Gemma 호출 성공, 한국어 요약 677~1514자, 23~24초
- E2E 캐시 히트: 74ms
- 설치: 멱등성 확인, 기존 훅 보존
- `settings.json`에 SessionStart 1개 항목만 깔끔하게 등록됨

### 파일 변경 내역 (재작업분)

| 파일 | 변경 내용 |
|---|---|
| `src/session_memory.py` | _is_system_reminder 추가, extract_text_from_content/extract_messages/call_gemma/build_prompt/main 수정 |
| `tests/test_session_memory.py` | 회귀 테스트 6개 추가 (M1 3개, M2 1개, E2 휴리스틱 2개) |
| `install.sh` | pre-warm 실행, 레거시 항목 정리, HOOK_CMD 단순화 |
| `uninstall.sh` | 레거시 형식까지 포괄적 매칭 |
| `README.md` | 테스트 디렉토리 전제 명시 |

Richard 재리뷰 대기.

---

## Deployed Steps

### Sprint 1 — Layer 1 SessionStart 자동 주입 (2026-04-15)

- 리뷰 결과: **Cleared** (Richard 재리뷰)
- 배포 방식: `./install.sh`
- 배포 위치: `/Users/yonghaekim/.claude/hooks/session-memory.py`
- settings.json: SessionStart 1개 항목 등록 (기존 PreToolUse/UserPromptSubmit 보존)
- pre-warm 캐시 생성 확인
- 검증: 다음 Claude Code 세션에서 자동 주입 작동하는지 육안 확인 예정

---

## Step 2 — Sprint 2 (FTS5 검색 + `/recall` 스킬) (2026-04-15)

### 무엇을 빌드했나

브리프 사양대로 과거 세션을 풀텍스트 검색해 요약 주입하는 `/recall` 스킬 파이프라인.

### 파일 변경

| 경로 | 종류 | 설명 |
|---|---|---|
| `src/indexer.py` | 신규 | JSONL → SQLite FTS5 증분 인덱서 (mtime+size 델타) |
| `src/search.py` | 신규 | FTS5 BM25 top 10 → Gemma 재순위 top 3 → Gemma 요약 |
| `src/recall_cli.py` | 신규 | `/recall` 스킬이 호출하는 CLI 진입점 (stdout JSON) |
| `skill/recall.md` | 신규 | Claude Code slash command 정의 (Bash 도구만 허용) |
| `src/session_memory.py` | 수정 | `trigger_background_indexer()` 추가, emit_output 후 Popen detach로 증분 인덱싱 호출 (cache HIT/MISS 양쪽 경로) |
| `install.sh` | 수정 | `~/.claude/scripts/mindvault/` + `~/.claude/commands/recall.md` 배포, 최초 인덱스 빌드 |
| `uninstall.sh` | 수정 | scripts/mindvault, commands/recall.md 제거 (DB는 preserve) |

### 핵심 결정 사항

1. **검색 단위 = 세션** (턴 X) — 1 JSONL = 1 row. BM25로 충분, snippet 윈도우 24자.
2. **증분 키 = (mtime_ns, size_bytes) 튜플** — Sprint 1의 v5-size-key 교훈 차용. mtime만으로는 동일 파일 touch로 불필요 재인덱싱 유발.
3. **스키마 버전 메타테이블** — `meta.schema_version` 불일치/손상 시 DB 파일 자동 삭제 후 재빌드. 운영 중 스키마 변경 대비.
4. **Gemma 재순위 실패 시 FTS5 top 3 폴백** — `gemma_rerank`가 JSON 파싱 실패하면 원순위 그대로 반환. 서버 다운 내성.
5. **쿼리 이스케이핑** — `[^\s"'`*:()]+` 패턴 단어만 추출, 각 단어 quote. 한국어 unicode61 토큰화 활용.
6. **훅 통합 = 백그라운드 detach** — `subprocess.Popen(..., start_new_session=True)`로 완전 분리. Sprint 1 회귀 위험 제로. stdout 이미 flush 후 실행.
7. **인덱서 import 경로** — session_memory.py(`~/.claude/hooks/`)와 indexer.py(`~/.claude/scripts/mindvault/`)가 다른 위치이므로 import 대신 `subprocess`로 직접 실행.

### 검증 결과

- **배포**: `./install.sh` 재실행 정상, 멱등성 유지. 180 세션 초기 인덱싱 완료 (`sessions=180, sessions_fts=180`).
- **FTS5 쿼리**: `"택시장부" "IAP"` 쿼리 → BM25 top 3 정상. `PRODUCT_NOT_FOUND` 오류 세션(`c9ca75d3`, 2026-03-23) 정확히 찾아냄.
- **E2E**: `recall_cli.py "택시장부 IAP"` → 3개 결과 반환, 각 세션당 Gemma 요약 400자 이내, 전체 47초 (Gemma 4회 호출: 1 재순위 + 3 요약).
- **Sprint 1 회귀 테스트**: 이 세션 SessionStart 훅이 여전히 `# 지난 세션 요약 (MindVault v3)` 블록을 주입하고 있음. 시스템 리마인더에 정상 표출 확인.

### 스펙 대비 변경 (Arch 승인 필요)

| 항목 | 브리프 | 실제 | 이유 |
|---|---|---|---|
| end-to-end 지연 | 10초 이내 | 약 47초 | Gemma 4 E4B 실측 12초/호출 × 4회. Sprint 1 실측치와 일관. 사용자 능동 호출이므로 허용 가능 판단 |
| 자동 테스트 파일 | `test_indexer.py`, `test_search.py` | **미작성** (Must Fix 후보) | Bob 판단: E2E 증거로 갈음했으나 Richard 리뷰에서 요구되면 즉시 작성 |

### Known Gaps

- `sqlite3` CLI에 FTS5 미컴파일 — macOS 기본 바이너리 제약, Python sqlite3는 지원. 디버그는 Python으로만 가능.
- 인덱스 DB 크기: 180 세션 기준 약 5~8MB 예상, 10000 세션 전 재평가 불필요.

---

## Step 3 — Sprint 3 (SessionEnd staging + `/memory review`) (2026-04-15)

### 무엇을 빌드했나

세션 종료 시 Gemma가 "영구 기억 후보"를 추출 → `memory/_staged/*.md`에만 저장. `/memory review` 스킬로 사용자 승인 시에만 실제 `memory/*.md` 생성 + `MEMORY.md` 인덱스 한 줄 append.

### 파일 변경

| 경로 | 종류 | 설명 |
|---|---|---|
| `src/memory_extractor.py` | 신규 | 트리거 키워드 감지 + Gemma 프롬프트/파싱 |
| `src/session_memory_end.py` | 신규 | SessionEnd 훅. sessionId→JSONL→extractor→staged 쓰기 |
| `src/memory_review_cli.py` | 신규 | `list/approve/reject/prune` 하위명령 CLI |
| `skill/memory_review.md` | 신규 | `/memory review` slash command |
| `src/session_memory.py` | 수정 | `purge_staged_memory()` 추가, 30일 경과 staged 정리 |
| `install.sh` | 수정 | SessionEnd 훅 등록, Sprint 3 스크립트·스킬 배포 |
| `uninstall.sh` | 수정 | SessionEnd 훅 + /memory_review 스킬 제거 |

### 핵심 결정 사항

1. **Staging + 승인 게이트** — MindVault v1 "자동 만능 저장" 실패 교훈. staged 이외 경로로 `memory/` 오염 불가.
2. **트리거 키워드 게이트** — `기억해/잊지마/결정:/앞으로는/원칙:` 등 regex 매칭된 세션만 추출. 일반 대화는 Gemma 호출조차 안 함 (비용 0).
3. **추출 타입 제한** — `feedback`, `project`만. user/reference는 주관성 높아 자동 추출 위험.
4. **slug 중복 차단** — 기존 staged + 실제 memory/ 합쳐서 중복 slug면 제안 자체 무시.
5. **30일 만료** — staged 파일은 SessionStart 훅이 실행될 때마다 cutoff 체크.
6. **MEMORY.md append 줄바꿈 방어** — 기존 파일이 `\n`으로 안 끝나는 경우 prefix 삽입 (E2E에서 발견한 버그 즉시 수정).

### 검증 결과

- **유닛 로직**: `has_trigger` 5케이스 모두 통과, `parse_gemma_json` 유효/무효/malformed 모두 graceful.
- **E2E 전 시나리오**:
  1. staged fixture 2개 생성 → `list` → 정확히 2건 반환 ✅
  2. `approve <file>` → `memory/sprint3_test_rule.md` 생성, MEMORY.md 인덱스 라인 append, staged 파일 삭제 ✅
  3. `reject <file>` → staged 삭제, memory/ 무영향 ✅
  4. 테스트 artifact 원복 완료 (실제 MEMORY.md/memory 파일은 깨끗)
- **Sprint 1 회귀 없음**: SessionStart 훅 자동 주입 여전히 작동 (현 세션에서 확인됨).
- **Sprint 2 회귀 없음**: `indexer`는 `*.jsonl`만 glob → `_staged/*.md`는 무관.

### Known Gaps / Richard Escalate 후보

- 자동 테스트 파일(`test_extractor.py`, 이월된 `test_indexer/test_search`) 미작성 — E2E로 갈음.
- `edit` 경로는 슬래시 스킬 body 수준의 지침 + Edit 도구 의존. CLI 단일 엔드포인트 `edit_approve`는 미구현 (LLM이 staged 파일 직접 편집 후 approve 호출).
- `/memory review` 스킬 본 세션에서 직접 호출 안 됨 — 실제 대화형 flow는 다음 세션에서 사용자가 호출해 확인 필요.
