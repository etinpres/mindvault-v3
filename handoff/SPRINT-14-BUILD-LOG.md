---
name: handoff-sprint14-build-log
description: Sprint 14 build log — Memory Compiler (LLM-as-compiler 패턴). SessionEnd 후보가 기존 memory 와 매칭되면 Gemma 로 정제·통합해 update_of 메타 부착, /memory_review diff + approve update flow 지원. opt-in MV3_AUTO_COMPILE=1
---

MindVault v3 → v3 Sprint 14 — Memory Compiler 빌드 로그

## 요약

Karpathy LLM-as-compiler 패턴 첫 구현. SessionEnd 에서 extractor 가 뽑은 후보를 그냥 staged 디렉토리에 던지지 않고, **기존 메모리와 매칭** 단계 추가. 매칭 있으면 Gemma 가 기존 본문 + 새 fact 를 통합해 정제된 update body 생성. 검토는 `/memory_review diff <file>` 으로 unified diff 보고 approve. approve 시 기존 파일 `.bak` 백업 후 overwrite.

master HEAD `87c7a09` (Sprint 13) 기준. opt-in: `export MV3_AUTO_COMPILE=1` 일 때만 활성. 기본 비활성 — 기존 v2.9.2 + Sprint 13 흐름 그대로 보존.

## 자율 결정 사유

- **Gemma vs Claude Sonnet** — V3-PLAN §3.B 의 옵션 비교에서 Gemma 우선 채택. 이유: 로컬·비용 0·SessionEnd 는 백그라운드라 latency 5~10s 허용 가능. Sonnet 호출 옵션은 미구현 — Sprint 17+ ship 시점에 형이 quality 부족 판단하면 그때 추가. 모듈 분리(`memory_compiler.py`)로 LLM 교체 비용 낮음.
- **매칭 알고리즘** — (1) frontmatter `name` 완전 일치 우선, (2) slug 일치 fallback. embedding 기반 의미 매칭은 도입 안 함. 이유: 동일 주제 → 동일 title → 동일 slug 로 형이 자연스럽게 수렴하는 패턴이라 keyword 충분. embedding 매칭은 false-merge 위험 (다른 주제 cosine 0.6 → 잘못된 update). 다음 sprint self-eval loop 에서 매칭 정확도 측정 후 필요 시 추가.
- **opt-in 환경변수** — V3-PLAN §4 의 점진 호환 원칙. 안정화 전 자동 fire 위험 — Gemma 가 기존 본문을 잘못 정제하면 형 메모리 손상. .bak 백업 + diff review 가 안전망이지만 default-off 가 더 안전. Sprint 17 ship 시점에 형 결정.
- **slug 일관성** — `memory_compiler.slugify` 가 `session_memory_end.slugify` 와 동등 룰. 한 곳에 두면 순환 import 위험이라 복제 + `TestSlugifyEquivalence` 로 동등성 보장.
- **`_call_gemma` 복제** — memory_extractor 의 함수를 import 안 하고 compiler 안에 같은 패턴 복제. 이유: extractor 가 후보 생성용(temperature 0.2, max 1500), compiler 가 정제용(temperature 0.2, max 800). 향후 prompt·token 정책 분기 가능성 위해 분리.

## 변경 상세

### A. `src/memory_compiler.py` 신규 (260 lines)

핵심 API:

```python
compile_candidates(candidates, memory_dirs=None) -> list[dict]
auto_compile_enabled() -> bool                   # env-based opt-in
unified_diff_text(old, new, context=2) -> str    # review CLI 용
diff_summary(old, new) -> str                    # +/- 카운트
```

내부 매칭/Gemma:

```python
_find_existing_memory(candidate, dirs) -> dict | None
  # 1순위: frontmatter name 완전 일치 (case-insensitive)
  # 2순위: slugify(title) == _candidate_slug(stem)
_compile_update(existing_body, candidate) -> str | None
  # Gemma prompt: 핵심 보존 + outdated 교체 + 정밀화 통합. 500자 hint, 1200자 hard cap.
  # markdown fence 자동 제거.
```

`_collect_md_files`, `parse_frontmatter`, `DEFAULT_MEMORY_DIRS`, `_extra_memory_dirs` 는 `memory_indexer` 에서 import — Sprint 13 의 `_procedural/` 슬롯도 자동 포함.

### B. `src/session_memory_end.py` 통합

`extract_from_jsonl` 직후 opt-in compile 단계 삽입:

```python
candidates = extract_from_jsonl(jsonl)
if not candidates:
    ...
    return 0

try:
    from memory_compiler import auto_compile_enabled, compile_candidates
    if auto_compile_enabled():
        candidates = compile_candidates(candidates)
        _debug(f"compiled session={sid[:8]} updates={N}/{total}")
except Exception as e:
    _debug(f"compile skipped: {e}")
    # 원본 candidates 그대로 사용
```

`write_staged` 가 candidate 의 `update_of` + `diff_summary` 메타 발견 시 staged frontmatter 에 보존.

### C. `src/memory_review_cli.py` 확장

- 신규 `cmd_diff(filename)`: staged 파일의 update_of 메타 보고 분기.
  - update flow: 기존 path 의 body + staged body unified diff JSON 출력
  - 신규 flow: staged body 만 표시
  - safety: `_is_safe_update_target` 가 update_of path 가 허용 root 안에 있는지 검증 (path traversal·임의 경로 방지)
- `cmd_approve` update flow 추가:
  - update_of 메타 있으면: `.bak` 백업 → 기존 frontmatter 의 name/description/type 보존 + body 만 정제본으로 교체
  - update_of 없으면: 기존 신규 promotion 흐름 그대로
  - update path 가 안전하지 않거나 사라졌으면 graceful fallback (신규 처리 또는 에러)
- 신규 sub `diff` 를 main router 에 등록. usage 메시지 갱신.

### D. 테스트 (`tests/test_memory_compiler.py` 신규, 21 tests)

| TestCase | 검증 |
|---|---|
| TestSlugifyEquivalence | `memory_compiler.slugify` == `session_memory_end.slugify` (6 cases) |
| TestDiffSummary | 빈 입력 처리 + add/remove 카운트 |
| TestUnifiedDiffText | `---`/`+++` 헤더 포함 |
| TestFindExistingMemory | name 매칭, slug fallback, name > slug 우선, no match |
| TestCompileCandidates | 빈 입력, 신규 통과, 매칭 → update, Gemma 실패 → 원본 보존, markdown fence 제거 |
| TestAutoCompileEnabled | 기본 off, "1" 만 on, "true"/"yes" off |
| TestSessionEndIntegration | write_staged 가 update_of/diff_summary 보존 |
| TestReviewCliUpdateFlow | cmd_diff (update/new), cmd_approve 가 .bak 생성 + 본문 교체 |

## 측정 데이터

### 신규 + 누적 테스트

```
tests/test_memory_compiler.py: 21/21 PASS (0.06s)
4-suite 회귀 + Sprint 13/14 신규: 94/99 PASS, 5 fail = pre-existing
  - test_embed_*: production embed_cache "hello" entry hit (master HEAD 동일)
  - test_returns_none_on_*: Gemma client mock isolation (master HEAD 동일)
누적 신규 테스트 (Sprint 13 + 14): 33건 추가
```

### 통합 시나리오 검증 (test_approve_update_writes_backup_and_overwrites)

```
시나리오: memory/topic.md 존재 + staged 후보가 update_of 보유
1. cmd_diff → unified_diff 에 "old body" + "new compiled body" 모두 표시
2. cmd_approve →
   - memory/topic.md.bak 생성 (기존 body)
   - memory/topic.md body 만 새것으로 교체
   - frontmatter name/description/type 기존 보존
   - staged 파일 unlink
   - reindex (mock) 호출
```

### Gemma fence 제거 검증

```
입력: "```\nv3 정제 본문\n```"
출력: "v3 정제 본문"  # head/tail fence 모두 제거
```

### 안전성 검증

- `_is_safe_update_target` 가 우리 메모리 root 외부 path 거부 → 임의 파일 overwrite 방지
- Gemma 응답 비어있거나 None 이면 원본 candidate 그대로 통과 — silent 정제 실패
- `_call_gemma` 예외는 None 반환 → 정제 미수행, extractor 결과 그대로 staged
- update path 가 사라졌으면 신규 promotion fallback (다른 경로 overwrite 안 함)

## 안전 정책 준수

- `indexer.full_rebuild()` 호출 없음.
- Sprint 10 트랜잭션 패턴 무변경.
- BGE plist + `bge_m3_server.py` 무변경.
- launchctl `com.yonghaekim.arctic-ko-mlx` 무관.
- 기본 비활성 (`MV3_AUTO_COMPILE` 미설정 시) — 형이 명시적으로 export 해야 발동.
- 기존 `memory/*.md` 자산은 .bak 백업 후에만 overwrite. approve 의 명시적 사용자 액션 + diff 검토 거침.
- worktree `v3-sprint-13-16` 격리 유지.

## 미해결 / Sprint 15+ 후보

- **embedding-based 의미 매칭** — 현재 keyword 기반 매칭은 형이 자연어 변형(예: "claude --bg" vs "백그라운드 세션") 으로 같은 주제 가리키면 못 잡음. Sprint 15 self-eval metric 으로 매칭률 측정 후 도입 여부 결정.
- **update 본문 길이 제어** — `COMPILE_BODY_LIMIT=500` 은 hint 일 뿐. Gemma 가 800+ 자 응답 시 hard cap 1200. v3 안정화 후 평균 길이 측정해 재튜닝.
- **diff 검토 UI 개선** — 현재 unified_diff_text 는 JSON 안에 raw 문자열. CLI 출력은 형이 직접 실행해 검토. `/memory_review diff <file>` 호출 시 색상 highlight 는 별도 작업 (현재 scope 밖).
- **conflict 처리** — 같은 session 안에서 동일 slug 후보가 둘 이상이면 마지막 것만 살아남음 (slugs dedup 기존 로직). compiler 가 동일 기존 path 와 매칭되면 staged 한 파일에 update_of 두 번 부착 → 두 번째만 staged. 안 위험하지만 손실 가능 — Sprint 15 metric 으로 빈도 측정.
- **production sync** — Sprint 13 과 동일. master 머지 후 install.sh 통한 일관 배포 권장 (현재 sprint 자체는 worktree → master fast-forward 만).

## 변경 파일

```
src/memory_compiler.py            | 신규 (260 lines)
src/session_memory_end.py         | +14 (compile 호출 + write_staged 메타)
src/memory_review_cli.py          | +110 (cmd_diff + approve update flow + safety)
tests/test_memory_compiler.py     | 신규 (350 lines, 21 tests)
handoff/SPRINT-14-BUILD-LOG.md    | 신규
```
