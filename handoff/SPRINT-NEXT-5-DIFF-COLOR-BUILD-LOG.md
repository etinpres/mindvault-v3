---
name: handoff-sprint-next-5-diff-color
description: V3-NEXT-IMPROVEMENTS #5 — memory_review_cli diff 의 --pretty 옵션. ANSI green/red/magenta 색상으로 +/-/hunk 시각화. JSON 출력 모드(파이프) 와 별도 plain text 출력.
---

MindVault v3 → 차기 보강 #5 — diff UI 색상 빌드 로그

## 요약

V3-NEXT-IMPROVEMENTS #5 해결. SPRINT-14-BUILD-LOG 미해결 3번. `memory_review_cli diff <file>` 는 unified_diff_text 결과를 JSON 안에 raw 문자열로만 전달했다. 형이 매 update approve 전에 diff 를 빠르게 훑어 정제 품질을 검토하는 비용을 줄이기 위해 ANSI 색상 highlight 옵션 추가.

master HEAD `77862c5` (NEXT-4 type gate) 기준 worktree `worktree-next-5-diff-color`.

## 자율 결정 사유

- **`--pretty` 명시 opt-in** — tty 자동 감지(`isatty`)는 안전하지 않다. 다른 도구가 stdout 파이핑 해서 JSON 파싱하는 자동화 흐름에서 우연히 isatty 가 True 가 되면 ANSI 가 섞여 들어가 깨진다. 형이 `diff <file> --pretty` 명시했을 때만 색상. JSON-RPC 호출 흐름은 변경 0.
- **ANSI 라인 단위 색상** — `+++/---` 헤더 = bold blue, `@@` hunk = magenta, `+` = green, `-` = red, context = 무색. git diff 의 관습 그대로.
- **update / new 모두 pretty 분기** — update 가 본 sprint 의 주 목표지만, new candidate 도 pretty 일 때 plain text 본문 보여주는 게 형 검토 일관성. `[new]`/`[update]` 헤더로 구분.
- **헤더 메타 표시** — `[update] <title>` + `  target: <update_of>` + `  summary: <diff_summary>` + `  <existing_len> → <compiled_len> chars` 로 diff 보기 전에 변경 규모 한눈 파악. JSON 모드와 동일 정보를 plain text 로.
- **argv parser 단순화** — argparse 안 쓰고 list comprehension 으로 `--pretty` 추출. 기존 4개 sub (list/diff/approve/reject/prune) 의 argv 처리 가벼움 유지. approve/reject 도 `rest[0]` 패턴 통일 — `--pretty` 옵션은 모든 sub 에서 무해하게 무시됨.

## 변경 상세

### A. `src/memory_review_cli.py`

- 새 상수: `ANSI_RESET`, `ANSI_RED`, `ANSI_GREEN`, `ANSI_MAGENTA`, `ANSI_BOLD_BLUE`.
- 새 헬퍼:
  - `_colorize_diff(diff_text) -> str`: 라인별 ANSI prefix/suffix 적용
  - `_should_use_color(pretty_flag) -> bool`: 현재 단순 `bool(pretty_flag)` — tty 자동 감지 의도적 비활성
- `cmd_diff(filename, pretty=False)` 시그니처 변경.
  - update_of 없으면: pretty 시 `[new] <title>\n\n<body>\n`, 아니면 기존 JSON
  - unsafe target: pretty 시 plain error 라인
  - update_of 있으면: pretty 시 `[update]` 헤더 + colored diff, 아니면 기존 JSON
  - 예외: pretty 시 plain `error: <msg>`
- `main()` 의 argv 처리: `--pretty` 분리 + diff/approve/reject 모두 `rest[0]` 패턴.

### B. 테스트 (`tests/test_memory_compiler.py` 의 새 `TestPrettyDiff`)

| 테스트 | 검증 |
|---|---|
| test_colorize_diff_plus_minus_hunks | `---`/`+++` bold blue, `@@` magenta, `+` green, `-` red, context 무색 |
| test_should_use_color_pretty_flag | `True`→True, `False`→False (tty 자동감지 OFF) |
| test_cmd_diff_pretty_for_update | update flow + pretty=True → JSON 아님, `[update]` 헤더 + ANSI green/red 포함 |
| test_cmd_diff_pretty_for_new_candidate | new flow + pretty=True → JSON 아님, `[new]` 헤더 + body 포함 |

## 측정 데이터

### memory_compiler 단독 (+ review cli)

```
29/29 PASS (0.12s)
신규 4건: TestPrettyDiff.*
기존 25건 보존 (TestSlugifyEquivalence, TestDiffSummary, TestUnifiedDiffText,
              TestFindExistingMemory, TestEmbeddingFallback, TestCompileCandidates,
              TestAutoCompileEnabled, TestSessionEndIntegration, TestReviewCliUpdateFlow)
```

### 전체 회귀

```
219/221 PASS (test_install_uninstall 제외, 101s)
2 fail = test_schema_v2.* — master HEAD `77862c5` 동일 pre-existing.
```

### 형의 사용 예 (운영)

```
# JSON (기존, 파이프 자동화)
python3 ~/.claude/scripts/mindvault/memory_review_cli.py diff 20260523-010101_feedback_topic.md

# 색상 plain text (형 직접 검토)
python3 ~/.claude/scripts/mindvault/memory_review_cli.py diff 20260523-010101_feedback_topic.md --pretty
```

## 안전 정책 준수

- `indexer.full_rebuild()` 호출 없음.
- Sprint 10 트랜잭션 패턴 무변경.
- BGE plist / `bge_m3_server.py` 무변경.
- launchctl 서비스 무관.
- JSON 출력 흐름 변경 0 — 기존 호출 caller 영향 없음.
- worktree 격리.

## 미해결 / 다음 #6~#7

- **tty 자동 감지 검토** — 현재 명시 opt-in. 형이 사용 패턴 확실해지면 `isatty` 자동 감지로 전환 가능 (단 stdout 리디렉션 자동화 흐름 무손실 확인 후).
- **diff context 라인 indent 표시** — context 라인은 무색이라 +/- 와 시각 구분되지만 indent 조정으로 더 명확해질 수도. 형 검토 피드백 후 결정.
- **#6 slug conflict, #7 scan latency** — 본 sprint 다음 사이클에서 순차 진행.

## 변경 파일

```
src/memory_review_cli.py                              | +75 -15
tests/test_memory_compiler.py                         | +110
handoff/SPRINT-NEXT-5-DIFF-COLOR-BUILD-LOG.md         | 신규
```
