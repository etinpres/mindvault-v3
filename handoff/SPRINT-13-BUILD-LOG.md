---
name: handoff-sprint13-build-log
description: Sprint 13 build log — procedural memory slot (memory/_procedural/) 신설, frontmatter type=procedural 신규 valid, memory_extractor trigger 패턴 확장(명령어 syntax workflow 환경설정), session_memory_end 라우팅, memory_review_cli 양쪽 슬롯 스캔, indexer hook _procedural 하위 인식
---

MindVault v3 → v3 Sprint 13 — Procedural Memory Slot 빌드 로그

## 요약

V3 첫 sprint. v2.9 한계 1.1 (procedural type 누락 — `claude --bg`, `git worktree add`, `launchctl load` 같이 형이 자주 검색·실험·발견한 명령어 syntax 가 메모리에 단 한 줄도 없는 문제) 해소. 결정 메모리(`memory/*.md`)와 분리된 절차적 메모리 슬롯 신설.

master HEAD `35c33f3` 기준 v2.9.2 위에서 작업. 이번 sprint 는 **저장 슬롯 + extractor trigger 확장만** — 검색·게이트·UI 는 기존 v2.9.2 로직 그대로 활용 (점진 호환 원칙). worktree branch `worktree-v3-sprint-13-16`.

## 자율 결정 사유

- **type 컬럼은 DB 에 추가 안 함** — frontmatter 만 가지고 디렉토리 단위로 분리. 마이그레이션 비용 0, 검색 시점 type 필터링은 다음 sprint 의 self-eval loop / classifier 가 필요할 때 추가. 지금 추가하면 사용처 없이 미사용 컬럼만 늘어남.
- **회수 게이트는 기존 raw_cosine_min 그대로 적용** — procedural 도 일반 메모리와 동일하게 검색됨. 디렉토리 분리는 저장·인벤토리·grep 목적. v3 의 다음 단계(self-eval) 에서 type 별 게이트 튜닝 여지 남김.
- **opt-in/opt-out 없음** — 기존 메모리 디렉토리가 비어 있어도 무해. trigger 가 발화돼야 Gemma 가 후보 만들고, 후보가 있어야 staged 슬롯이 만들어진다. 기존 자산 무손실.

## 변경 상세

### A. `_procedural/` 슬롯 신설 (`src/memory_indexer.py`, `hooks/memory-recall.py`)

`_collect_md_files` 가 각 memory root 의 `_procedural/` 하위까지 스캔. 기존 `_staged` 제외 로직(`any(part == "_staged" for part in p.parts)`)이 `_procedural/_staged/` 도 자동으로 가린다.

```python
candidates: list[Path] = list(d.glob("*.md"))
proc_dir = d / PROCEDURAL_SUBDIR
if proc_dir.is_dir():
    candidates.extend(proc_dir.glob("*.md"))
```

`memory-recall.py` 의 `_mtime_changed()` 도 동일하게 `_procedural/` 하위 mtime 도 watch — 절차 메모리 추가 시 hook 이 자동 reindex spawn.

### B. type=procedural 처리 (`src/memory_extractor.py`)

- `VALID_TYPES = ("feedback", "project", "procedural")` 추가.
- trigger regex 확장: 명령어·syntax·workflow·환경설정 키워드 그룹. 한국어 + 영어 혼합.

```python
TRIGGER_RE = re.compile(
    r"(기억해|잊지\s?마|...|규칙[:：]|"
    r"이 명령어|이 명령은|이렇게\s?(?:쓰면|하면|입력하면|실행하면)|"
    r"syntax|문법|이\s?(?:패턴|workflow|플로우|순서|절차)는?|"
    r"외워둬|기억해둬|반복(?:해서|적으로)\s?(?:쓰|할|사용|실행)|"
    r"환경설정|환경\s?변수|셋업|setup|이\s?(?:flag|옵션|플래그))"
)
```

- Gemma prompt 에 `type 가이드` 섹션 추가 — feedback/project/procedural 구분 + procedural 의 body 포맷(실행 예시 1줄 + 한 줄 설명) 명시. 예시: `claude --bg "prompt" # 백그라운드 세션 시작`.

### C. staged 라우팅 (`src/session_memory_end.py`)

- `PROCEDURAL_DIR = MEMORY_DIR / "_procedural"`, `PROCEDURAL_STAGED_DIR = PROCEDURAL_DIR / "_staged"` 추가.
- `staged_dir_for(memory_type)` 신규: procedural → PROCEDURAL_STAGED_DIR, 그 외 → STAGED_DIR.
- `write_staged()` 가 type 보고 라우팅. 디렉토리는 lazily mkdir.
- `existing_slugs()` 가 4곳(MEMORY_DIR, STAGED_DIR, PROCEDURAL_DIR, PROCEDURAL_STAGED_DIR) 모두 스캔 — 슬롯 분리 후에도 dup 방지.

### D. review CLI 확장 (`src/memory_review_cli.py`)

- `STAGED_DIRS = (STAGED_DIR, PROCEDURAL_STAGED_DIR)` — list/approve/reject/prune 모두 양쪽 스캔.
- `_safe_staged_path()` 가 두 슬롯에서 차례로 lookup. path traversal 방어 유지.
- `_promote_target_dir(meta_type)`: procedural → PROCEDURAL_DIR, 그 외 → MEMORY_DIR. `cmd_approve` 가 staged 파일의 type meta 보고 promoted 위치 결정.
- `cmd_prune` 도 양쪽 디렉토리 순회.

## 측정 데이터

### 신규 테스트 (Sprint 13)

`tests/test_procedural_slot.py` 신규 + `tests/test_memory_indexer.py` 에 2건 추가.

```
tests/test_procedural_slot.py: 12/12 PASS (0.02s)
  TestExtractorProceduralType: 5/5
  TestSessionEndStagedSlot: 4/4
  TestReviewCliRoutes: 3/3
tests/test_memory_indexer.py 신규: 2/2 PASS
  test_procedural_subdir_indexed: _procedural/ 하위 인덱싱 확인
  test_procedural_staged_excluded: _procedural/_staged/ 제외 확인
```

### 회귀 테스트 (4 suite 합산)

```
Ran 61 tests in 33.24s — FAILED (failures=5)
```

5건 fail 은 모두 **master HEAD `35c33f3` 에서도 동일**한 pre-existing test isolation 결함 — production embed_cache 의 "hello"/Gemma cache entry 가 mock 보다 우선 hit. Sprint 11 BUILD-LOG §"미해결" 4번에 명시된 사안. Sprint 13 변경 무관.

- `test_embed_timeout_returns_none`
- `test_embed_bad_shape_returns_none`
- `test_embed_connection_refused_returns_none`
- `test_returns_none_on_connection_error` (Gemma client)
- `test_returns_none_on_timeout` (Gemma client)

확인: 동일 file 들을 `git checkout 35c33f3 --` 로 되돌린 뒤 두 test class 만 실행 → 동일 5/5 fail. 

### Trigger 패턴 매칭 검증 (test_procedural_slot)

| 입력 | 매칭 | 비고 |
|---|---|---|
| `이 명령어 외워둬: claude --bg` | ✓ | 복합 trigger |
| `이 syntax 자주 쓰니까 기억해둬` | ✓ | 영어 syntax + 한글 |
| `이렇게 하면 백그라운드 실행돼` | ✓ | workflow 패턴 |
| `환경설정 한 줄: export ...` | ✓ | env 키워드 |
| `이 옵션 외워둬` / `이 flag 자주 쓴다` | ✓ | flag 패턴 |
| `안녕하세요 오늘 날씨 어떄요` | ✗ | 잡담 (false positive 없음) |
| `그냥 이거 한번 돌려봐` | ✗ | 일회성 |
| `테스트 결과 어떻게 됐어?` | ✗ | 진행 보고 |

### 라우팅 검증

`write_staged` 가 type 별로 분기:
- `type=procedural` → `_procedural/_staged/20YYMMDD-HHMMSS_procedural_<slug>.md`
- `type=feedback` / `type=project` → `_staged/...` (기존 동작 보존)

`cmd_approve` 가 promote 분기:
- procedural slot 의 staged → `_procedural/<slug>.md`
- 일반 staged → `<slug>.md`

## 안전 정책 준수

- `indexer.full_rebuild()` 호출 없음.
- Sprint 10 트랜잭션 패턴 (매 iter conn.commit + embed_text reordering) 무변경.
- BGE plist + `bge_m3_server.py` 무변경 (롤백 경로 유지).
- launchctl `com.yonghaekim.arctic-ko-mlx` 무관.
- 작업은 worktree `v3-sprint-13-16` 격리.
- 기존 `memory/*.md` 자산 무변경 — 새 slot 은 추가만.

## 미해결 / Sprint 14+ 후보

- **자동 trigger 휴리스틱 부재** — 현재는 명시적 키워드 trigger 만. Sprint 14 Memory Compiler 가 LLM 으로 raw transcript → procedural 항목 자동 추출하면 본격적 자동화. 현재 sprint 는 인프라(슬롯·라우팅)만 깔아둠.
- **type 별 회수 게이트 분리** — 절차 메모리는 일반 결정보다 specific keyword 매칭이 강해야 정확함. Sprint 15 self-eval loop 에서 metric 보고 결정.
- **production sync 는 master 머지 후 별도 단계** — Sprint 13 commit 자체는 worktree → master fast-forward 만. `~/.claude/scripts/mindvault/` 와 `~/.claude/hooks/memory-recall.py` 운영 위치 sync 는 형 깨면 install.sh 통한 일관 배포 권장.

## 변경 파일

```
hooks/memory-recall.py            | +8
src/memory_indexer.py             | +18 -3
src/memory_extractor.py           | +14 -5
src/session_memory_end.py         | +15 -4
src/memory_review_cli.py          | +50 -14
tests/test_memory_indexer.py      | +34
tests/test_procedural_slot.py     | 신규 (170 lines)
handoff/SPRINT-13-BUILD-LOG.md    | 신규
```
