---
name: handoff-architect-brief-sprint3
description: Sprint 3 (Layer 3) architect brief — SessionEnd 훅으로 Gemma가 기억 후보를 staged 폴더에 저장하고 /memory review 슬래시 명령으로 사용자 승인 받은 항목만 영구 MEMORY.md에 등록하는 승인 게이트 설계
---

# Architect Brief — MindVault v3 (Sprint 3 of 3 = Layer 3)

*Written by Architect. Read by Builder and Reviewer.*

**From:** Arch → **To:** Bob (Builder)
**Date:** 2026-04-15
**Sprint:** 3 (SessionEnd 훅 → staged 기억 후보 → `/memory review` 승인 게이트)

---

## Goal (한 줄)

세션 종료 시 Gemma가 "영구 기억 후보"를 추출해 **staging 폴더에만** 저장하고, 사용자가 `/memory review`로 승인한 항목만 실제 `MEMORY.md` 시스템에 들어간다.

---

## Success Criteria (테스트 가능)

1. **기능**: 세션에 "기억해/잊지마/결정:" 등 트리거 키워드가 있으면 SessionEnd 훅이 Gemma를 호출해 후보를 `memory/_staged/`에 md 파일로 남긴다.
2. **억제**: 트리거 키워드가 없거나 대화가 짧으면 훅은 조용히 패스 (빈 staged, 비용 0).
3. **승인 게이트**: `/memory review` 호출 시 staged 파일 목록을 보여주고, 사용자 선택에 따라 실제 `memory/*.md`로 이동 + `MEMORY.md` 인덱스 한 줄 추가.
4. **MindVault v1 실패 방지**: 승인 전까지 어떤 자동 저장도 `MEMORY.md` / 실제 memory 파일을 건드리지 않음.
5. **만료**: `_staged/` 파일은 생성 시점부터 30일 경과하면 SessionStart 훅에서 자동 삭제.
6. **Sprint 1/2 회귀**: `_staged/*.md`는 SessionStart 자동 주입과 FTS5 인덱스 둘 다에서 제외 (노이즈 방지).

---

## Scope — IN

- SessionEnd 훅 파이썬 스크립트 (`session-memory-end.py`)
- 키워드 트리거 감지 (feedback/project 대상만 추출)
- Gemma 추출 프롬프트 + 구조화 출력 파싱 (JSON)
- `memory/_staged/` 디렉토리 관리 + 30일 만료
- `/memory review` 스킬 + CLI (`memory_review_cli.py`) — 대화형 승인
- `MEMORY.md` 인덱스 라인 append (user 프로필 타입별 section 존중)
- `install.sh` / `uninstall.sh` 확장
- 기본 단위 테스트 (Sprint 2에서 연기된 `test_indexer.py`, `test_search.py` 포함)

## Scope — OUT

- user/reference 타입 자동 추출 (너무 주관적 → 수동으로 유지)
- 기존 memory 파일 수정/삭제 (append-only)
- 다른 프로젝트 디렉토리 대상
- UI, 웹 인터페이스

---

## Deliverables

```
apps/mindvault-v3/
├── src/
│   ├── session_memory.py         # (기존) _staged 만료 자동 청소 1줄 추가
│   ├── session_memory_end.py     # [NEW] SessionEnd 훅
│   ├── memory_extractor.py       # [NEW] Gemma 프롬프트 + JSON 파싱
│   ├── memory_review_cli.py      # [NEW] /memory review 진입점
│   ├── indexer.py                # [기존] _staged 디렉토리 스킵
│   └── ...
├── skill/
│   ├── recall.md                 # (기존)
│   └── memory_review.md          # [NEW] slash command
├── tests/
│   ├── test_extractor.py         # [NEW]
│   ├── test_indexer.py           # [NEW, Sprint 2 이월]
│   └── test_search.py            # [NEW, Sprint 2 이월]
├── install.sh                    # SessionEnd 훅 등록, 스킬 배포 추가
└── uninstall.sh
```

---

## Technical Spec

### 1. 트리거 키워드 (extractor.py)

추출 대상 세션 판별 — 하나라도 매칭되면 추출 실행:
```
기억해, 잊지마, 잊지 마, 잊지말아, 결정[:：], 정했[어다], 앞으로는, 다음부턴, 이 프로젝트는, 원칙[:：], 규칙[:：]
```

매칭 로직: 세션 JSONL 마지막 30턴 내 사용자(type=user) 메시지에서 정규식 검색. 매칭 없으면 즉시 `exit 0`.

### 2. SessionEnd 훅 입력

Claude Code는 SessionEnd 훅에 다음 JSON을 stdin으로 전달:
```json
{"sessionId": "...", "cwd": "...", "reason": "exit|logout|..."}
```

스크립트는:
1. `sessionId`로 JSONL 경로 확정 → `~/.claude/projects/-Users-yonghaekim-my-folder/{sid}.jsonl`
2. 트리거 키워드 없으면 종료
3. 마지막 40턴 추출, PII 마스킹, Gemma에 전달

### 3. Gemma 추출 프롬프트

```
아래는 Claude Code 세션 대화 일부다. 사용자가 "영구 기억"으로 남기려고 한 사실만
추출하라. 주관적 의견·일회성 대화·진행 상황 보고는 제외.

출력은 JSON 배열만. 각 항목:
{
  "type": "feedback" | "project",
  "title": "한 줄, 50자 이내",
  "body": "본문, 200자 이내, markdown 허용",
  "reason": "왜 저장할 가치가 있는지 10자 이내",
  "evidence": "원문에서 인용 30자"
}

후보가 없으면 빈 배열 [].
절대 해설·마크다운 코드펜스 금지. JSON 배열만.

---대화---
{excerpt}
---끝---
```

Gemma 출력 파싱:
- `re.search(r"\[[\s\S]*\]", out)`로 JSON 블록 추출 후 `json.loads`
- 실패하면 빈 배열 처리 (조용히 패스)
- 각 항목 필수 필드 검증, 누락 시 스킵

### 4. Staging 파일 쓰기

경로: `/Users/yonghaekim/.claude/projects/-Users-yonghaekim-my-folder/memory/_staged/`

파일명: `{YYYYMMDD-HHMMSS}_{type}_{slug}.md` — slug는 title을 snake_case 30자 이내.

파일 내용:
```markdown
---
name: {title}
description: {title}
type: {type}
staged_at: {ISO timestamp}
staged_from_session: {session_id[:8]}
reason: {reason}
evidence: {evidence}
---

{body}
```

중복 방지: 기존 staged + 실제 memory 디렉토리에서 title slug 충돌 시 제안 무시 (이미 저장됨).

### 5. `/memory review` 스킬

스킬 body (`skill/memory_review.md`):
```
사용자가 `/memory review`를 호출했다.

1. Bash 도구로 실행:
   python3 /Users/yonghaekim/.claude/scripts/mindvault/memory_review_cli.py list

2. 결과 JSON {"staged":[{"file":"...","type":"...","title":"...","body":"...","reason":"...","age_days":N}]}
   - 0개면: "승인 대기 중인 기억 후보 없음." 출력 후 끝.
   - 있으면 각 항목을 번호 매겨서 보여주고, 마지막에 "각 항목에 대해 [y]승인/[n]폐기/[e]편집/[s]건너뛰기 중 하나로 답해줘" 안내.

3. 사용자 응답을 받은 후 항목별로:
   - y → python3 .../memory_review_cli.py approve {file}
   - n → python3 .../memory_review_cli.py reject {file}
   - e → 사용자에게 어떤 필드 편집할지 묻고, 완료 후 approve
   - s → 아무것도 안 함

4. 결과 요약: 승인 N, 폐기 M, 건너뜀 K.
```

CLI 하위명령:
- `list` → JSON 출력
- `approve {file}` → staged 파일의 frontmatter/body를 실제 memory/에 복사, MEMORY.md에 인덱스 라인 append, staged 파일 삭제
- `reject {file}` → staged 파일 삭제
- `prune` → 30일 경과 staged 삭제 (SessionStart 훅에서 호출)

### 6. MEMORY.md 인덱스 라인 포맷

실제 memory/ 파일로 이동 시 `MEMORY.md` 본문 끝에 다음 한 줄 append:
```
- [{title}]({slug}.md) — {reason}
```

섹션 자동 삽입은 하지 않음 (사용자가 수동 재분류하도록 — 인덱스 폭증 방지).

### 7. Sprint 1 자동 주입과의 경계

- `session_memory.py` (Sprint 1)는 JSONL만 읽음 → staged 영향 없음.
- 단, `session_memory.py`에 `_staged/` 30일 만료 청소 한 줄 추가:
  ```python
  try:
      from pathlib import Path as _P
      _staged = _P("/Users/yonghaekim/.claude/projects/-Users-yonghaekim-my-folder/memory/_staged")
      if _staged.is_dir():
          cutoff = time.time() - 30*86400
          for f in _staged.glob("*.md"):
              if f.stat().st_mtime < cutoff:
                  f.unlink()
  except Exception:
      pass
  ```

### 8. Sprint 2 인덱서 보호

`indexer.py`는 `-Users-yonghaekim-my-folder/*.jsonl`만 보므로 `memory/_staged/`는 자동 제외. 추가 작업 없음. 단 테스트로 확인.

### 9. 에러 처리

SessionEnd 훅·memory_review_cli 모두 Sprint 1/2 규약 계승:
- Gemma 다운 → 빈 staged 리스트 + exit 0
- JSONL 손상·파싱 실패 → exit 0
- MEMORY.md 쓰기 실패 → staged 파일 유지 (재시도 가능)
- 절대 stderr·exit 1 금지

### 10. settings.json 등록

기존 Sprint 1 SessionStart와 동일 패턴으로 SessionEnd 배열에 append:
```json
{
  "matcher": "*",
  "hooks": [{"type": "command", "command": "/Users/yonghaekim/.claude/hooks/session-memory-end.py"}]
}
```

install.sh 파이썬 스니펫에 SessionEnd 블록 확장. 기존 SessionStart 보존 멱등성 유지.

---

## 참고 자료

- **Sprint 1/2 구현**: `apps/mindvault-v3/src/session_memory.py`, `indexer.py` — PII 마스킹·JSONL 파싱 함수 재사용
- **Gemma 호출**: `src/search.py::call_gemma` 그대로 재사용 가능
- **기존 MEMORY.md 구조**: `/Users/yonghaekim/.claude/projects/-Users-yonghaekim-my-folder/memory/MEMORY.md` — 섹션 유지, 한 줄 append만

---

## 제약 및 경고

1. **MindVault v1 실패 패턴 재발 절대 금지** — 승인 없는 자동 저장 없음. staged가 실제 memory/를 오염시키면 안 됨.
2. **보수적 추출** — Gemma가 "될까 말까"하면 버려라. 1 false positive가 10 true negative보다 나쁨.
3. **staged 파일도 읽기 가능 포맷** — 형이 Finder에서 열어봐도 의미 통해야 함.
4. **CARL 규칙** — 절대 경로, 병렬 tool call, 완료 전 검증 (실제 `/memory review` E2E까지).
5. **Sprint 1/2 회귀 금지** — staged 파일이 SessionStart 요약·FTS5 검색에 끼어들면 안 됨.
6. **Sprint 2 이월 테스트도 이번에 작성** — `test_indexer.py`, `test_search.py` 간소하게.

---

## 테스트 요구사항 (Richard 리뷰 통과 조건)

### 자동 테스트

1. `test_extractor.py`:
   - 트리거 키워드 없는 세션 → 빈 결과
   - 명시적 "기억해: X" 세션 → 최소 1개 후보 (Gemma mock)
   - Gemma malformed 출력 → 빈 결과, crash 없음
   - 중복 title 차단 확인

2. `test_indexer.py` (이월):
   - fixtures 3개 인덱싱, 재실행 스킵, mtime 변경 시 upsert
   - `_staged/` 디렉토리가 존재해도 인덱서가 JSONL만 처리

3. `test_search.py` (이월):
   - Gemma mock으로 재순위 파이프라인 검증
   - Gemma 다운 시 raw_snippet 폴백

### 수동 E2E

1. `./install.sh` 재실행 → SessionStart + SessionEnd 둘 다 정상 등록, 기존 Sprint 1/2 보존.
2. 현재 세션에 "기억해: 커피 테스트" 한 줄 포함 → 세션 종료 → `memory/_staged/`에 md 파일 생성 확인.
3. 새 세션 → `/memory review` → 후보 목록 출력 → `y` 응답 → 실제 `memory/커피_테스트.md` 생성 + MEMORY.md 마지막 줄 추가 확인.
4. `reject` 경로 → staged 파일만 사라지고 memory/ 영향 없음 확인.
5. Gemma 중단 상태에서 SessionEnd → `debug.log`에 기록되지만 아무것도 저장 안 됨.
6. Sprint 1 회귀: 새 세션 열 때 자동 주입 블록 여전히 작동.
7. Sprint 2 회귀: `/recall 커피` 테스트 → staged 파일이 인덱스에 안 들어가 있음 확인.

---

## Bob에게 — 다음 액션

1. 브리프 재확인. 특히 "승인 게이트" 개념 — 어떤 경로로도 자동 저장 금지.
2. Sprint 1/2 함수 재사용: PII 마스킹, JSONL 파서, Gemma client.
3. `memory_extractor.py` → `session_memory_end.py` → `memory_review_cli.py` → `skill/memory_review.md` 순 구현.
4. Sprint 2 이월 테스트 2개도 함께 작성.
5. `./install.sh` 재검증 (멱등성·SessionStart 보존), 수동 E2E 전 시나리오 실행.
6. `BUILD-LOG.md` Step 3 추가, `REVIEW-REQUEST.md` 갱신.
