---
name: handoff-sprint-next-1-auto-trigger
description: V3-NEXT-IMPROVEMENTS #1 — memory_extractor 자동 trigger 휴리스틱. assistant Bash tool_use 의 special_binary / non_trivial 명령어 + 직후 user 의 NEXT_ACTION 응답을 결합해 명시 키워드 없이도 procedural 후보를 Gemma 에게 넘긴다. session_memory_end 의 production path 우선순위 버그 동시 수정.
---

MindVault v3 → 차기 보강 #1 — 자동 trigger 휴리스틱 빌드 로그

## 요약

V3-NEXT-IMPROVEMENTS.md 의 7개 후보 중 **#1 자동 trigger 휴리스틱** 해결. Sprint 13 procedural slot 인프라는 완성됐지만 `TRIGGER_RE` 가 명시 키워드 (`기억해`, `이 명령어`) 발화만 매칭해 자동 추출이 사실상 0건이었다 — `self_eval --procedural-audit --hours 720` 측정 시 695 Bash / 42 binary / **procedural 메모리 coverage 0.0%** (covered: 0).

master HEAD `2aeedb1` 기준 worktree `worktree-next-1-auto-trigger` 에서 작업. 휴리스틱 1개 + path 우선순위 버그 fix.

## 자율 결정 사유

- **휴리스틱 = special_binary OR non_trivial + NEXT_ACTION** — 형의 실제 jsonl 응답 패턴을 샘플링한 결과 "ok/굿" 같은 confirmation 은 거의 없고 "진행/적용/켜줘/영구화" 형태의 다음 액션 지시가 주류 (5~43자 짧은 메시지). 따라서 confirmation 매칭이 아니라 next-action 매칭으로 설계.
- **임계값 50자** — 형의 응답 패턴 최장(43자) + 약간의 여유. 100자급 잡담은 next_action 키워드가 섞여 있어도 trigger 안 켬.
- **special_binary 화이트리스트만** — 처음엔 "처음 본 binary" trigger 도 고려했으나 procedural memory 가 0건이라 모든 binary 가 신규로 잡혀 false trigger 폭증. 대신 `launchctl|sqlite3|ffprobe|ffmpeg|yt-dlp|higgsfield|kubectl|gcloud|hyperframes|jq|awk|sed -i|gh api|claude --bg/-c/--resume|git worktree` 만 화이트리스트. python3/git/ls 같은 일상 binary 는 non_trivial 게이트(길이 100+ 또는 pipe/redirect 2+) 통과해야 trigger.
- **Gemma 가 최종 판별 안전망 유지** — trigger 가 켜져도 Gemma 가 procedural 후보 안 만들면 staged 안 생성. trigger ON 의 비용은 Gemma 호출 1회(로컬, 비용 0)만.
- **session_memory_end path 우선순위 버그 fix 동봉** — 회귀 검증 중 발견. `session_memory_end.py` 가 `sys.path.insert(0, "~/.claude/scripts/mindvault")` 를 무조건 추가해 worktree 의 새 함수 (extract_bash_from_content 등) 가 production 캐시에 의해 가려지는 케이스. dev 모드 자동 감지 (`memory_extractor.py` 가 자기 옆에 있으면 dev) 로 fix. 한 번 발견된 이상 격리 sprint 로 미루지 않고 같이 해결.

## 변경 상세

### A. 새 정규식·헬퍼 (`src/memory_extractor.py`)

- `SPECIAL_BIN_RE`: 화이트리스트 binary 매칭.
- `NEXT_ACTION_RE`: `진행|해결|적용|켜줘|실행|영구화|반영|배포|sync|push|land|merge|commit|ship|다음|이어서|계속`.
- `_is_non_trivial_bash(cmd)`: 길이 ≥ 100 OR `" | "` 카운트 ≥ 2 OR `>`/`>>` 합 ≥ 2.
- `_is_special_bash(cmd)`: `SPECIAL_BIN_RE.search`.

### B. `extract_bash_from_content` (신규)

```python
def extract_bash_from_content(content) -> list[str]:
    """assistant message 안의 Bash tool_use command 문자열만 수집."""
    ...
```

assistant tool_use 중 `name="Bash"` 만 추출, `input.command` 에서 redact 후 반환. tool_result(user role)·다른 도구는 무시.

### C. `load_tail_messages` 확장

기존: text 비면 skip. 변경: text 비어도 bash_commands 가 있으면 entry 추가. 각 entry 에 `bash_commands: list[str]` 필드 첨부.

### D. `has_trigger` 휴리스틱

```python
prev_bash_signal = False
for m in messages:
    if role == "assistant":
        if any(_is_special_bash(c) or _is_non_trivial_bash(c) for c in cmds):
            prev_bash_signal = True
        continue
    # user 처리
    if TRIGGER_RE.search(text): return True
    if prev_bash_signal and len(text) <= 50 and NEXT_ACTION_RE.search(text):
        return True
    prev_bash_signal = False  # user turn 종료 = signal reset
```

기존 키워드 trigger 와 OR 결합. 한 user turn 안에서 assistant 가 tool_use → text 로 분할돼도 signal 누적(중간 assistant 메시지가 bash 없어도 reset 안 함). user turn 마침이 곧 reset 트리거.

### E. `build_prompt` 에 bash 첨부

각 메시지의 `bash_commands` (최대 5개, 명령어당 300자) 를 `A:bash: <command>` 형식으로 excerpt 에 포함. Gemma 가 procedural type 추출 시 실제 명령어 직접 보고 판단.

### F. `session_memory_end.py` path 우선순위 fix

기존:
```python
for _p in (production, _HOOK_FILE.parent):
    if _p.is_dir() and str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
```
production 무조건 sys.path[0]. dev/repo 가 이미 sys.path 에 있으면 dev 추가 skip → production 이 우선.

변경:
```python
_HOOK_DIR = _HOOK_FILE.parent
if (_HOOK_DIR / "memory_extractor.py").is_file():
    if str(_HOOK_DIR) not in sys.path:
        sys.path.insert(0, str(_HOOK_DIR))
else:
    _PROD = Path("/Users/yonghaekim/.claude/scripts/mindvault")
    if _PROD.is_dir() and str(_PROD) not in sys.path:
        sys.path.insert(0, str(_PROD))
```
자기 옆에 `memory_extractor.py` 있으면 (dev/repo 또는 정상 배포된 production) 그쪽만 등록. 없으면 (hooks/ 만 단독 배포된 hook 파일) production fallback. dev / production 양쪽 동작 보존.

## 측정 데이터

### 신규 테스트 (test_procedural_slot.py 안)

```
TestAutoTriggerHeuristic: 8/8 PASS
  test_special_bash_then_next_action_triggers
  test_non_trivial_bash_then_next_action_triggers
  test_trivial_bash_does_not_trigger
  test_special_bash_without_next_action_does_not_trigger
  test_long_next_action_message_does_not_trigger  # 64자 잡담 차단
  test_signal_resets_after_user_turn
  test_signal_accumulates_across_split_assistant
  test_text_trigger_still_works_with_new_message_shape

TestExtractBashFromContent: 3/3 PASS
  test_extracts_bash_command
  test_ignores_non_bash_tools
  test_redacts_secrets  # Bearer ... → [REDACTED]

TestBuildPromptIncludesBash: 1/1 PASS
  test_bash_lines_appended
```

### 회귀 (worktree 전체)

```
198 tests in 100s — 2 failed (test_schema_v2 pre-existing — schema_version 가 3 인데 테스트는 2 기대)
```

master HEAD `2aeedb1` 에서 `tests/test_schema_v2.py` 단독 실행 → 동일 2/4 fail 확인. 본 sprint 변경 무관.

이전 회귀 (`session_memory_end` fix 전): 9 fail (procedural_slot 7건 + schema_v2 2건). fix 후 2 fail. 신규 회귀 0건.

### audit (변경 전후 baseline)

```
$ self_eval.py --procedural-audit --hours 720
{
  "total_bash_commands_examined": 695,
  "unique_binaries": 42,
  "procedural_memory_count": 0,
  "coverage_ratio": 0.0
}
```
변경 후 동일 (procedural 메모리는 형이 review 후 approve 한 시점에 늘어남). 본 sprint 는 trigger 게이트만 풀어둔 단계 — 실효 검증은 Memory Compiler ON 상태에서 며칠 후 sessions 가 누적된 다음 동일 audit 재실행해 coverage > 0 확인.

## 안전 정책 준수

- `indexer.full_rebuild()` 호출 없음.
- Sprint 10 트랜잭션 패턴 무변경.
- BGE plist / `bge_m3_server.py` 무변경 (롤백 경로 보존).
- launchctl 서비스 (`com.yonghaekim.arctic-ko-mlx`, `com.yonghaekim.gemma-mlx`, `com.yonghaekim.mv2-env`) 무관.
- worktree `next-1-auto-trigger` 격리.
- 기존 `memory/*.md` / `_procedural/*.md` 자산 무변경.
- Sprint 17 ship 관련 자동 제안 금지 — 본 build log 끝에 다음 sprint 권유 없음.

## 미해결 / 다음 #2~#7 후보

- **휴리스틱 측정 부재** — Memory Compiler ON 상태에서 며칠 누적 후 audit coverage 변화 + Gemma 가 만든 procedural candidate 의 staged 통과율을 측정해야 임계값 (50자 / non_trivial 100자 / SPECIAL_BIN 화이트리스트) 튜닝 근거가 생긴다. V3-PLAN.md §5 표에 NEXT-1 행 추가했지만 실측 수치는 미정.
- 빈 자리: 다른 #2~#7 항목 (embedding 매칭, Gemma 보강 classifier, type 별 게이트, diff 색상, slug conflict, scan latency 캐시) 모두 미해결. 형이 별도 지시할 때 진행.

## 변경 파일

```
src/memory_extractor.py                          | +60 -5
src/session_memory_end.py                        | +9 -4
tests/test_procedural_slot.py                    | +135
handoff/SPRINT-NEXT-1-AUTO-TRIGGER-BUILD-LOG.md  | 신규
```
