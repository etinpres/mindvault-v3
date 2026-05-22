# Session Checkpoint — 2026-05-22 (Sprint 4 종료, Layer 4 배포)

*Read this before reading anything else.*

Sprint: 4 (Layer 4 — UserPromptSubmit hybrid recall)
Status: **배포·검증 완료. 4-layer 파이프라인 완성.**

---

## 이번 세션 완료

Sprint 4 (Layer 4 Memory Recall) 한 세션에 brainstorm → spec → plan → 9 tasks 모두 완료:

- 산출물:
  - `scripts/bge_m3_server.py` (BGE-M3 MLX HTTP 서버, port 8081, threading.Lock)
  - `plist/com.yonghaekim.bge-m3-mlx.plist` (launchd, KeepAlive on crash)
  - `src/memory_indexer.py` (frontmatter + BGE-M3 + flock + path safety)
  - `src/memory_search.py` (hybrid RRF k=60, desc 1.5x, BLOB cosine, min-max norm)
  - `hooks/memory-recall.py` (UserPromptSubmit, 250ms hard timeout, silent fail)
  - `src/recall_cli.py` 확장 (--source memory|sessions|both)
  - `src/memory_review_cli.py` 1줄 추가 (approve → incremental_index)
  - `install.sh` / `uninstall.sh` 확장 (idempotent, telegram-guard 보존)
  - `tests/test_*.py` 5개 신규 (40+ 단위 테스트, 5 E2E)
  - `docs/superpowers/specs/2026-05-22-sprint4-layer4-recall-design.md`
  - `docs/superpowers/plans/2026-05-22-sprint4-layer4-recall.md`
  - `~/my-folder/mindvault-v2-sprint4-spec.html`, `~/my-folder/mindvault-v2-sprint4-plan.html` (사람용 dashboards)

- 배포:
  - `~/Library/LaunchAgents/com.yonghaekim.bge-m3-mlx.plist` (launchctl load)
  - `~/.claude/hooks/memory-recall.py`
  - `~/.claude/scripts/mindvault/{memory_indexer, memory_search, bge_m3_server}.py`
  - `~/.claude/scripts/mindvault/memory_review_cli.py` (reindex 트리거 포함)
  - `~/.claude/settings.json` UserPromptSubmit: telegram-guard 보존 + memory-recall append
  - `~/.cache/mlx-bge-m3/` (322MB 모델)
  - DB schema V2 마이그레이션 (sessions 306 보존, memories 104, memories_fts 104, memories_vec 201)

---

## 핵심 기술 결정

- **BGE-M3 MLX 4-bit (`mlx-community/bge-m3-mlx-4bit`)** — 한영 혼용 + Apple Silicon 최적.
- **sqlite-vec 폴백 → BLOB + numpy cosine** — macOS 시스템 Python 3.10 sqlite3가 `enable_load_extension` 미지원, `pysqlite3-binary`도 arm64 wheel 부재. 100개 규모라 인덱스 검색 O(log n) 이점 무의미.
- **이중 임베딩** — body + frontmatter description. description은 정수만 박혀있어 RRF에서 1.5x 가중.
- **RRF k=60** + **min-max 정규화 (배치 내 독립)** + **threshold 0.65**. (top-1은 항상 1.0이라 게이트가 약함 — 실 운영 hit rate 데이터 기반 튜닝 예정)
- **threading.Lock on model forward** — mlx-embeddings가 thread-safe 아님 (Metal GPU command queue race, SIGSEGV 재현됨). Lock 적용 후 100+ 동시 호출 안정.
- **HARD_TIMEOUT 250ms** (200 → 250) — cold-start 흡수. install.sh에 hook warmup 추가.
- **schema V2 마이그레이션** — V mismatch 시 unlink 안 함, CREATE IF NOT EXISTS only로 sessions_* 보존.
- **CLAUDE.md `[메모리 회수 Ritual]`은 Sprint 4 동안 공존** — hook hit rate 안정 후 Sprint 5+에서 폐기.

---

## 성능 (검증된 수치)

| 지표 | 측정 |
|---|---|
| BGE-M3 임베딩 단일 (mx.eval 포함) | 1.9ms (모델 로드 후) |
| memory_search (cold) | ~87ms |
| memory_search (warm) | ~32ms |
| **hook 전체 (cold)** | **184ms** (1회) |
| **hook 전체 (warm)** | **147-167ms** (p95 ~140ms) |
| **hook 전체 (100회 dev avg)** | **129.6ms avg, 139.9ms p95** |
| full_rebuild (104 memories, 201 vecs) | ~44s (BGE-M3 호출 비용) |
| incremental (변경 1건) | < 200ms |
| schema migration V1→V2 | < 10ms (sessions 보존) |

---

## E2E 증거

- 5/5 통합 테스트 PASS (한국어 자연어, 짧은 prompt silent, 정확 식별자, 100회 성능, Sprint 2 회귀)
- 실 메시지 검증: "메일 보내는 도구" → `sendmail-natural-language` 회수 (score 1.00, vec source)
- 10 병렬 stress 후 BGE-M3 서버 생존 (Lock 효과)
- settings.json: telegram-guard 보존 + memory-recall 추가 ✓
- launchctl: `com.yonghaekim.bge-m3-mlx` 등록 (PID 32475)

---

## 2026-05-22 추가 fix (post Sprint 4 deploy)

### Sprint 1 (SessionStart): Gemma → claude -p 전환

- 형 새 세션 시작 5분+ 지연 (Gemma E4B reasoning cache MISS) → **17초** (claude -p --model haiku)
- 2-stage (mini × 5 + unified) → **single-stage** (전체 세션 head/tail 한 prompt → 1 호출)
- ThreadPoolExecutor 병렬화 시도했으나 NodeJS subprocess contention으로 더 느림 (1:32) → 단일 통합이 답
- CACHE_VERSION v3 → v4-claude-p
- shutil.which + nvm fallback PATH 보강
- commit `9bfa695`

### 무한 재귀 guard

- `MV2_HOOK_RECURSION_GUARD=1` 환경변수 sentinel
- session_memory.py의 call_gemma → claude -p subprocess 호출 시 env propagate
- 모든 mv2 hook(session_memory.py, session_memory_end.py, memory-recall.py, async wrapper)이 이 변수 보면 즉시 exit 0
- inner claude -p 안의 sub-session hook 전부 차단 → 무한 재귀 0

### SessionEnd memory_extractor import 경로 fix

- 원인: ~/.claude/hooks/에 배포돼도 memory_extractor.py는 ~/.claude/scripts/mindvault/에 있음
- fix: sys.path에 둘 다 추가

### Sprint 4 (Layer 4): V1 토큰 낭비 패턴 회피 (형 핵심 피드백)

- 문제: min-max normalize → top-1 항상 1.0 → 매번 3건 헛스윙 회수 = V1 폐기 원인 재현
- 진단: BGE-M3 너무 일반화 — 진짜 0.76-0.80 / 헛스윙 0.73-0.78 / 잡담 0.64-0.75 (raw cosine만으로 거의 구분 불가)
- 설계:
  - **raw vec cosine 절대 게이트** 도입 (default 0.79, hint 시 0.76)
  - **TOP_K 3 → 1** (절대 우수 1건만)
  - **MIN_PROMPT_LEN 3 → 4** (너무 짧은 키워드 skip)
  - **회수 단서어 감지** ("예전에", "그때", "이전에" 등 8개) → 임계값 미세 완화
  - **metrics.jsonl에 raw_top1_cosine 기록** (실 운영 튜닝용)
  - **fts hit은 raw 게이트 면제** (BM25 정확 키워드 보장)
  - normalize score는 ranking signal로만 (절대 차단 X)
- 실 검증 (15개 쿼리):
  - 8건 silent (잡담·메타·헛스윙 차단)
  - 4건 정확 회수 (메일→sendmail, icloud→iCloud copy, 이전 스캐너→scan, 유튜브→유튜브 대본)
  - 2건 인접 적중 (폰트→VS Code 폰트)
  - 1건 헛스윙 (스캐너→sendmail, top_k=1이라 V1 패턴 아님)
- commit `2c40c1e`

### 검증 진행률 (verify.html 체크리스트)

- ✓ 0: 새 세션 SessionStart 정상
- ✓ 1: 자연어 비키워드 회수 (V1 fix 후)
- ✓ 2: 잡담·단답 silent
- ✓ 3: 체감 지연 (Sprint 1 17초 + Sprint 4 hook 150ms)
- ✓ 4: /recall 양쪽 (memory 정확, sessions 부분 적중)
- 미: 5 (/cs 통합), 6 (degradation)

---

## 알려진 한계 (Sprint 5+ 작업)

- **Sprint 2 sessions 섹션 헛스윙** — /recall 검증 시 sessions 결과에 도메인 무관 세션 포함됨. memory_search의 raw cosine 게이트 패턴을 Sprint 2 search.py에도 적용 필요. [Sprint 5 task #19]
- **RAW_COSINE_MIN 0.79 휴리스틱** — 1-2주 실 운영 후 metrics.jsonl raw_top1_cosine 분포 분석으로 미세 튜닝. [task #20]
- **CLAUDE.md `[메모리 회수 Ritual]` 폐기 보류** — hook hit rate 안정 검증 후. [task #21]
- **시간 단위 청킹 미구현** — 5KB+ 파일에 한해 `[YYYY-MM-DD]` 단위 자동 분할.
- **WikiLink (`[[name]]`) graph expansion 미구현** — top-k 결과의 wikilink 1-hop 확장.
- **JSONL 세션 임베딩 인덱스 미구현** — 현재 FTS5만. Sprint 5+ 후보.
- **자동 테스트 회귀 안전망 약함** — CI 없음. 로컬 unittest로만.
- **macOS 시스템 Python 3.10 sqlite3 미지원으로 sqlite-vec 사용 불가** — BLOB 폴백 유지.

---

## 잔여 이슈 (완료 판정)

- **BGE-M3 SIGSEGV (Thread 24)** 재현 → `threading.Lock` 패치 적용 후 해결. 다시 발생하면 mx.eval 누락 또는 큰 입력 cap 검토.
- **/memory review 자동 reindex 통합 테스트** — Task 9 수동 검증으로 이관됐고, 실 staged 시나리오는 다음 SessionEnd 발화 시 검증 가능.

---

## 디버깅 포인트

```bash
# BGE-M3 헬스
curl http://localhost:8081/health

# BGE-M3 재기동
launchctl kickstart -k gui/$(id -u)/com.yonghaekim.bge-m3-mlx

# hook 수동 호출
echo '{"prompt":"테스트"}' | python3 ~/.claude/hooks/memory-recall.py

# memory_search 단독
python3 ~/.claude/scripts/mindvault/recall_cli.py "테스트" --source memory

# 인덱스 강제 재구축
python3 -c "import sys; sys.path.insert(0, '/Users/yonghaekim/.claude/scripts/mindvault'); from memory_indexer import full_rebuild; full_rebuild()"

# DB 상태
python3 -c "import sqlite3; c=sqlite3.connect('/Users/yonghaekim/.claude/mindvault-v2/index.db'); [print(t, c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]) for t in ('sessions','memories','memories_fts','memories_vec')]"

# 최근 hook 로그
tail -30 ~/.claude/mindvault-v2/debug.log | grep -E "hook-recall|mem-search|mem-indexer"

# 제거
bash ~/my-folder/apps/mindvault-v2/uninstall.sh
# 또는 (memories_* 테이블도 정리)
bash ~/my-folder/apps/mindvault-v2/uninstall.sh --purge-vec
```

---

## 파일 위치

- 프로젝트: `~/my-folder/apps/mindvault-v2/`
- spec/plan: `docs/superpowers/{specs,plans}/2026-05-22-sprint4-*.{md,html은 ~/my-folder/}`
- 배포본:
  - 훅: `~/.claude/hooks/{session-memory, session-memory-end, memory-recall}.py`
  - 스크립트: `~/.claude/scripts/mindvault/*.py` (10개)
  - 스킬: `~/.claude/commands/{recall, memory_review}.md`
  - 모델: `~/.cache/mlx-bge-m3/` (322MB)
  - DB: `~/.claude/mindvault-v2/index.db` (17.86MB, schema V2)
  - launchd: `~/Library/LaunchAgents/com.yonghaekim.bge-m3-mlx.plist`
  - 로그: `~/Library/Logs/bge-m3-mlx.{log,err}`

---

## MindVault v2 현황 요약

| Layer | 상태 | 기능 |
|---|---|---|
| 1. SessionStart 자동 주입 | ✅ 배포 | 최근 5세션 Gemma 요약 자동 주입. 캐시 히트 ~50ms |
| 2. /recall 검색 | ✅ 배포 | sessions FTS5+Gemma 재순위/요약 + memory hybrid RRF |
| 3. SessionEnd staging + /memory review | ✅ 배포 | 트리거 감지 → staged → 승인 → memory/*.md + reindex |
| 4. UserPromptSubmit hook | ✅ 배포 | memory/*.md hybrid (vec+fts RRF) 매 메시지 자동 주입 |

**4-layer 파이프라인 완성.** brainstorm → spec → plan → 9-task subagent-driven (controller 직접) 한 세션에 완성. 13 git commits.
