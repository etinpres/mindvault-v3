# MindVault v3 Sprint 4 — Layer 4 Memory Recall (Hybrid)

- **Date**: 2026-05-22
- **Author**: brainstorming session, yonghaekim + Claude (Opus 4.7)
- **Status**: Design approved, awaiting user spec review
- **Predecessor sprints**: Sprint 1 (SessionStart 자동 주입), Sprint 2 (FTS5 + /recall), Sprint 3 (SessionEnd staging + /memory review) — 모두 배포 완료 (2026-04-15)

---

## 1. 목적

`memory/*.md`에 증류해둔 메모리를 **자연어 의미 매칭으로 자동 회수**해 매 사용자 메시지 컨텍스트에 주입한다. 사용자가 키워드를 외우거나 회수 단서어를 쓰지 않아도, 발화 자체가 검색 쿼리가 된다.

### 풀려는 문제

1. **현재 사각지대**: 기존 인덱서는 JSONL 세션 로그만 본다. 형이 `/cs`로 증류해 정리한 핵심 메모리(`memory/*.md`, 약 100개)는 검색 대상이 아니다. 가장 회수가치가 높은 자산이 그동안 의미 매칭 검색에서 빠져 있었다.
2. **FTS5 토크나이저 한계**: `unicode61` 토크나이저는 한국어 형태소 분석을 하지 않는다. "메일 보냈더라" 같은 활용형은 "msmtp"·"sendmail" 키워드를 가진 메모리에 매칭되지 않는다.
3. **인지 부담**: 현행 [메모리 회수 Ritual] 규칙(`CLAUDE.md`)은 키워드 매칭 + 회수 단서어 휴리스틱에 의존한다. 형이 적절한 단어를 발화해야 회수가 발동하므로 자연스러운 대화 흐름에서 자주 누락된다. (Sprint 4 동안은 ritual과 hook 자동 회수가 **공존** — ritual 폐기는 hook hit rate 안정 확인 후 Sprint 5+에서.)
4. **수동 트리거**: `/recall` 슬래시 명령은 형이 명시 호출해야 한다. 백그라운드 자동 회수가 없다.

### 성공 기준

- "예전에 그 폰트 깨졌던 거" 같은 비키워드 자연어 발화에서 `project_hyperframes_fonts_fix` 메모리가 자동으로 회수된다.
- 잡담·단답(`고마`, `ㅇㅇ`, `잠깐`)에서는 회수가 발동하지 않는다 (산만함 방지).
- hook 평균 응답 < 100ms, p95 < 200ms (체감 지연 없음).
- 기존 Sprint 1/2/3 동작에 회귀 없음.
- BGE-M3 서버 다운 등 임의 실패가 사용자 메시지 처리를 절대 블로킹하지 않는다 (silent fail).

---

## 2. 핵심 결정 매트릭스

| 변수 | 결정 | 근거 |
|---|---|---|
| 검색 대상 | `memory/*.md` 만 (~100 파일, 616KB) | 증류 자산이 회수가치 최고. JSONL은 기존 Layer 2 담당 |
| 자동화 수준 | UserPromptSubmit hook 자동 주입 | 인지 부담 해소가 핵심 동기 |
| 임베딩 모델 | BGE-M3 MLX (4-bit 양자화) | 한영 혼용 메모리 특성. 기존 Gemma MLX와 같은 패턴 |
| 청킹 | 파일 통째 + frontmatter `description` 이중 인덱스 | description 필드가 정수만 박혀있어 매칭 정밀도 ↑ |
| hook 발동 조건 | 모든 메시지 + cosine 0.65 임계값 게이트 | 잡담 자연 필터 + 누락 최소화 |
| 검색 알고리즘 | 임베딩 + FTS5 RRF (k=60) | 의미 매칭(활용형) + 정확 식별자(함수명) 둘 다 잡음 |
| 인덱싱 트리거 | lazy incremental (mtime 비교) + `/cs` approve 시 명시 호출 | 백그라운드 데몬 불필요. 기존 indexer.py 패턴 재사용 |
| 구현 스코프 | Sprint 4에 한 번에 완성 (분할 안 함) | 부분 구현은 의미 없음. 하루 내 완료 가능한 양 |

---

## 3. 아키텍처

기존 3-layer 위에 4번째 Layer를 **순수 추가**한다. 기존 컴포넌트 무손상.

```
┌───────────────────────────────────────────────────────────┐
│                  MindVault v3 (existing)                  │
│                                                            │
│  Layer 1: SessionStart  ─→  최근 5세션 Gemma 요약 주입    │
│  Layer 2: /recall       ─→  JSONL FTS5 + Gemma 재순위/요약 │
│  Layer 3: SessionEnd    ─→  staged → /memory review       │
└───────────────────────────────────────────────────────────┘
                              │
                              │ 새 메모리 추가/수정
                              ▼
                    memory/*.md (~100 files)
                              │
            ┌─────────────────┴──────────────────┐
            ▼                                     ▼
┌─────────────────────────┐         ┌────────────────────────┐
│   sqlite-vec 인덱스     │         │   FTS5 인덱스          │
│   (BGE-M3 1024차원)     │         │   (memories_fts)       │
│   kind ∈ {body, desc}   │         │   body 전체            │
└─────────────────────────┘         └────────────────────────┘
            └─────────────────┬──────────────────┘
                              ▼
              ┌─────────────────────────────┐
              │   memory_search.py (NEW)     │
              │   RRF 결합 + 정규화 + 임계값 │
              └─────────────────────────────┘
                              │
                              ▼
              ┌─────────────────────────────┐
              │  hooks/memory-recall.py      │
              │  (UserPromptSubmit, 매 메시지)│
              └─────────────────────────────┘
                              │
                              ▼
                       Claude 컨텍스트
                       (system-reminder)
```

**신규 인프라**:
- `~/Library/LaunchAgents/com.yonghaekim.bge-m3-mlx.plist` (포트 8081)
- `~/.claude/hooks/memory-recall.py`
- `~/.claude/scripts/mindvault/memory_indexer.py`, `memory_search.py`
- `index.db`에 `memories`, `memories_fts`, `memories_vec` 테이블 3개 추가 (기존 `sessions_*` 무손상)

---

## 4. 컴포넌트 명세

### 4.1 `bge_m3_server` (launchd 서비스)

| 항목 | 값 |
|---|---|
| 위치 | `~/Library/LaunchAgents/com.yonghaekim.bge-m3-mlx.plist` |
| 포트 | 8081 (Gemma 8080과 분리) |
| 모델 | `mlx-community/bge-m3-mlx-4bit` (4-bit 양자화, ~322MB) |
| 인터페이스 | `POST /embed {"input": str}` → `{"vector": [1024 floats]}` |
| 기동 | launchd `KeepAlive=true`, 부팅 시 자동, 크래시 시 재시작 |
| 의존성 | `mlx-embeddings` 또는 `mlx-lm` 패키지 |
| 헬스체크 | `curl http://localhost:8081/embed -d '{"input":"test"}'` |

Gemma 서비스(`com.yonghaekim.gemma-mlx`) 패턴 그대로 복사한다.

### 4.2 `src/memory_indexer.py` (NEW)

**책임**: `memory/*.md` → sqlite-vec + FTS5 이중 인덱싱.

**공개 함수**:
```python
def incremental_index(memory_dirs: list[Path] | None = None) -> dict[str, int]:
    """mtime 비교로 변경된 파일만 재임베딩.
    반환: {"updated": N, "skipped": M, "removed": K}
    """

def full_rebuild(memory_dirs: list[Path] | None = None) -> int:
    """DB 드롭 후 전체 재인덱싱. install.sh 초기 실행, 스키마 마이그레이션 시 호출."""
```

**인덱싱 단위**: 파일당 2 row
- `kind='body'`: 파일 본문 전체 (frontmatter 제거 후)
- `kind='description'`: frontmatter `description` 필드 (없으면 skip)

**데이터 소스 (기본값)**:
- `~/.claude/projects/-Users-yonghaekim/memory/*.md`
- `~/.claude/projects/-Users-yonghaekim-my-folder/memory/*.md`
- `_staged/` 디렉토리는 제외

**의존**: BGE-M3 서버 (HTTP), 기존 `indexer.py`의 `redact()` 재사용 (secret 마스킹).

**lock**: `~/.claude/mindvault-v3/memory-indexer.lock` (flock LOCK_NB, 동시 실행 차단).

### 4.3 `src/memory_search.py` (NEW)

**책임**: hybrid RRF 검색.

**공개 함수**:
```python
def recall_memory(
    query: str,
    top_k: int = 3,
    score_threshold: float = 0.65,
) -> list[dict]:
    """
    반환:
    [{
        "path": str,           # 절대경로
        "name": str,           # frontmatter name
        "description": str,    # frontmatter description
        "snippet": str,        # body 발췌 1~2줄 (FTS5 snippet 또는 첫 N자)
        "score": float,        # 정규화 0~1
        "source": list[str],   # ["vec"], ["fts"], ["vec","fts"]
    }, ...]
    """
```

**알고리즘**:
1. 쿼리 임베딩 (`POST /embed`, ~30ms)
2. sqlite-vec에서 cosine top-10 — `kind='description'` 결과는 `1/(60+rank) * 1.5` 가중
3. FTS5 BM25 top-10 — body 매치 그대로
4. RRF 결합: `score = Σ (가중 적용된 1/(60+rank))` — k=60 표준
5. path 단위로 점수 집계 (한 파일이 vec+fts 양쪽에서 hit하면 합산)
6. **min-max 정규화**: 현재 검색 배치 내 최대 점수를 1.0, 최소를 0.0으로 선형 변환 → `score_threshold` 게이트 → top_k 반환. (글로벌 정규화 X — 매 쿼리마다 독립)

**threshold=0** 호출 가능 (`/recall` 명시 호출 시).

### 4.4 `~/.claude/hooks/memory-recall.py` (NEW)

**책임**: UserPromptSubmit hook. 매 사용자 메시지 직전 발동.

**입력**: stdin JSON `{"prompt": str, "session_id": str}`

**출력**: stdout. 회수 결과가 있으면 system-reminder 포맷, 없으면 빈 출력.

**흐름**:
1. prompt 길이 < 3자 → 즉시 `exit 0`
2. lazy mtime 체크 (~5ms): memory/ 최신 mtime > 인덱스 mtime이면 백그라운드 spawn `python3 -c "from memory_indexer import incremental_index; incremental_index()" &` (결과 기다리지 않음)
3. `recall_memory(prompt, top_k=3, score_threshold=0.65)` 호출
4. 결과 비면 `exit 0`
5. 결과 있으면 포맷팅:
   ```
   <system-reminder>
   # 메모리 회수 (Layer 4 hybrid)
   - **<name>** (score 0.78, vec+fts) — <description>
     발췌: <snippet>
   - ...
   </system-reminder>
   ```
6. 전 과정에 200ms hard timeout (signal.alarm 또는 자체 타이머)
7. 모든 예외는 try/except 최상위 → `debug.log` + `exit 0`

### 4.5 `src/recall_cli.py` (확장)

**현재**: FTS5 세션 검색만.

**확장**: `--source memory|sessions|both` 플래그 추가.
- `memory`: `recall_memory(threshold=0.0)` 호출 (신규)
- `sessions`: 기존 동작 유지 (변경 없음)
- `both` (default): 둘 다 호출 → 섹션별 표시

기존 `/recall` 슬래시 스킬은 인자 없이 호출되므로 `both`로 자연스럽게 통합.

### 4.6 `install.sh` / `uninstall.sh` (확장)

**install 추가 작업**:
- BGE-M3 launchd plist 생성 + `launchctl load`
- UserPromptSubmit hook 등록 (`settings.json` `hooks.UserPromptSubmit` 배열에 append, `.bak` 백업)
  - **기존 hook 보존 의무**: 형 환경에는 이미 `telegram-guard` hook이 등록되어 있음. 절대 덮어쓰지 말 것 (`tests/test_install_uninstall.py`에서 명시 검증)
- `pip install mlx-embeddings sqlite-vec` (또는 pyproject 업데이트)
- 초기 `full_rebuild()` 실행
- 헬스체크: BGE-M3 응답 확인

**uninstall 추가 작업**:
- `launchctl unload` + plist 삭제
- settings.json UserPromptSubmit 배열에서 해당 항목 제거 (다른 hook 보존)
- 옵션: `--purge-vec` 플래그 시 `memories_*` 테이블 drop

### 4.7 `src/memory_review_cli.py` (소폭 수정)

`approve` 명령 마지막에 `memory_indexer.incremental_index()` 트리거 1줄 추가. staged → memory/ 이동 직후 자동 재임베딩.

### 4.8 의존성 그래프

```
bge-m3-mlx (launchd, HTTP)
       ▲ HTTP
       │
memory_indexer.py ──→ sqlite-vec, FTS5
       │
       ▼
memory_search.py (RRF)
       │
       ├──→ hooks/memory-recall.py (UserPromptSubmit)
       └──→ recall_cli.py (--source memory)
```

**삭제·deprecate 없음**: Sprint 4는 순수 추가.

---

## 5. 데이터 흐름

### 5.1 인덱싱 (lazy incremental)

**트리거**:
- T1: UserPromptSubmit hook 진입 시 mtime 변화 감지 (백그라운드 spawn)
- T2: `/memory review` approve 직후 (foreground)
- T3: `install.sh` 초기 1회 (foreground, `full_rebuild`)
- T4: 수동 `python3 -m memory_indexer` (디버깅)

**파일 단위 처리**:
```
변경된 .md 파일
   │
   ├─→ parse frontmatter (name, description)
   ├─→ extract body (frontmatter 이후)
   ├─→ redact() (secret 마스킹)
   ├─→ POST /embed (body) → vec_body
   ├─→ POST /embed (description) → vec_desc  [description 있을 때만]
   ├─→ DELETE FROM memories WHERE path=?
   ├─→ DELETE FROM memories_fts WHERE path=?
   ├─→ DELETE FROM memories_vec WHERE path=?
   ├─→ INSERT INTO memories (path, name, description, mtime_ns, indexed_at)
   ├─→ INSERT INTO memories_fts (path, body)
   ├─→ INSERT INTO memories_vec (kind='body', path, embedding=vec_body)
   └─→ INSERT INTO memories_vec (kind='description', path, embedding=vec_desc)
```

**삭제 처리**: DB에 있는 path 중 디스크에 없는 것 → 모든 테이블에서 DELETE.

**예상 비용** (100 파일 변경 시): BGE-M3 200회 × 30ms ≈ 6초. 실 변경은 1~5건이라 < 200ms.

### 5.2 회수 (hook 발동)

```
사용자 메시지 입력
   │
   ▼
UserPromptSubmit hook stdin 전달
   │
   ▼
prompt 길이 < 3 ─Yes─→ exit 0
   │ No
   ▼
mtime 비교 ─변경─→ incremental_index() 백그라운드 spawn (continue)
   │
   ▼
POST /embed (쿼리)
   │
   ▼
┌──────────────┬──────────────┐
▼              ▼
sqlite-vec     FTS5
top-10 cosine  top-10 BM25
(desc 1.5x)
   └──────┬───────┘
          ▼
   RRF 결합 (k=60)
   path별 점수 집계
          ▼
   min-max 정규화
          ▼
   ≥ 0.65 필터
          ▼
   top-3 ─0개─→ exit 0
          │
          ▼
   <system-reminder> 포맷
          ▼
   stdout 출력 → Claude 컨텍스트
```

### 5.3 `/recall` 통합 흐름

```
/recall <query> [--source memory|sessions|both]
   │
   ▼
recall_cli.py 파싱
   │
   ├─ memory  → memory_search.recall_memory(threshold=0)  top-5
   ├─ sessions → 기존 FTS5 + Gemma 재순위 (변경 없음)        top-3
   └─ both    → 위 둘 다 호출, JSON 섹션 분리
   │
   ▼
JSON stdout → /recall 슬래시 스킬이 사람용 포맷팅
```

### 5.4 SQLite 스키마 (추가분)

기존 `sessions`, `sessions_fts` 무손상. 같은 `index.db` 안에 공존.

```sql
CREATE TABLE memories (
    path        TEXT PRIMARY KEY,
    name        TEXT,
    description TEXT,
    mtime_ns    INTEGER NOT NULL,
    indexed_at  TEXT NOT NULL
);

CREATE VIRTUAL TABLE memories_fts USING fts5(
    path UNINDEXED,
    body,
    tokenize='unicode61 remove_diacritics 2'
);

-- NOTE (2026-05-22 환경 발견): macOS 시스템 Python 3.10의 sqlite3가
-- enable_load_extension을 지원하지 않아 sqlite-vec(vec0) 사용 불가.
-- pysqlite3-binary도 arm64 wheel 없음. → BLOB + Python cosine 폴백.
-- 메모리 자산이 ~100개라 인덱스 검색의 O(log n) 이점은 의미 없음.
CREATE TABLE memories_vec (
    rowid     INTEGER PRIMARY KEY AUTOINCREMENT,
    path      TEXT NOT NULL,
    kind      TEXT NOT NULL,         -- 'body' | 'description'
    embedding BLOB NOT NULL          -- float32 numpy bytes, 1024 * 4 = 4096 bytes
);

CREATE INDEX idx_memories_vec_path ON memories_vec(path);
```

**schema_version 2로 bump** → 기존 V1 DB는 자동 unlink + full_rebuild 트리거. sessions 테이블은 마이그레이션으로 보존 (CREATE IF NOT EXISTS만 사용, DROP 안 함).

---

## 6. 에러 처리 & Fail-safe

### 6.1 철학

MindVault v3 1~3 Layer의 **"절대 세션을 블로킹하지 않는다"** 정신을 그대로 계승. 모든 실패는 silent → `exit 0` 빈 출력.

### 6.2 실패 시나리오 매트릭스

| 시나리오 | 동작 | 사용자 영향 |
|---|---|---|
| BGE-M3 서버 다운 | 30ms connection refused → exit 0 | 회수만 빠짐, Claude 정상 응답 |
| BGE-M3 응답 200ms+ | hook 타임아웃 → 결과 폐기 | 동일 |
| sqlite-vec 미설치/로드 실패 | indexer 시작 시 시도, 실패 시 FTS5만으로 운영 | hybrid → FTS5 only degrade |
| index.db 손상 | schema_version 체크 실패 → unlink + full_rebuild | 첫 호출 ~20초 지연 |
| frontmatter 파싱 실패 | description="" 로 인덱싱 (body는 정상) | 해당 파일은 description 매치만 누락 |
| 디스크 쓰기 실패 | indexer return, debug 로그 | 다음 trigger에서 재시도 |
| hook 자체가 throw | try/except 최상위 → exit 0 | 사용자 모름 |
| 사용자 메시지 매우 김 (10KB+) | BGE-M3가 8192 토큰 cap에서 truncate | 정상 동작 |
| 동시 indexer 실행 | flock(LOCK_NB) → 한쪽만 작업 | 다른 쪽 skip |
| /cs approve 중 인덱서 실패 | approve 성공, 임베딩 보류. 다음 hook 진입 시 lazy reindex로 만회 | 새 메모리 회수 1턴 지연 |

### 6.3 Hard Rules (불가침)

1. **사용자 메시지 처리 블로킹 0** — hook 200ms 초과 / crash / exception 시 즉시 exit 0
2. **memory/ md 수정 0** — Layer 4는 read-only. 쓰기는 Layer 3 (memory_review_cli) 전담
3. **JSONL 세션 손상 0** — `sessions_*` 테이블 절대 건드리지 않음
4. **stdout 오염 0** — 디버그/에러는 무조건 debug.log, stdout은 회수 결과 외 0
5. **path traversal 차단** — Sprint 3 `_safe_staged_path()` 패턴 재사용

### 6.4 관찰성

`~/.claude/mindvault-v3/debug.log`에 prefix로 출처 구분:
```
[2026-05-22 14:32:01] hook-recall: query='...' candidates=5 picked=2 elapsed_ms=87
[2026-05-22 14:32:15] mem-indexer: updated=3 skipped=98 elapsed_ms=312
[2026-05-22 14:32:30] hook-recall: bge_m3 timeout 200ms, skip
[2026-05-22 14:33:00] mem-search: vec_hits=10 fts_hits=8 rrf_top=[file_a, file_b, file_c]
```

추가 `metrics.jsonl` (hit rate 분석용):
```json
{"ts": "2026-05-22T14:32:01", "kind": "recall", "query_len": 42, "elapsed_ms": 87, "picked": 2, "max_score": 0.81}
```

### 6.5 Degradation 우선순위

1. 임베딩 + FTS5 hybrid (이상)
2. BGE-M3 다운 → FTS5만 (degrade)
3. sqlite-vec 미설치 → FTS5만 (설치 안내 1회 출력)
4. index.db 손상 → 첫 호출 자동 full_rebuild
5. memory/ 디렉토리 없음 → Layer 4 작동 안 함, 다른 Layer 정상

### 6.6 디버깅 가이드 (README 추가용)

```bash
# BGE-M3 헬스체크
curl http://localhost:8081/embed -d '{"input":"테스트"}' -H 'Content-Type: application/json'

# BGE-M3 재기동
launchctl kickstart -k gui/$(id -u)/com.yonghaekim.bge-m3-mlx

# hook 수동 실행
echo '{"prompt":"테스트 쿼리"}' | python3 ~/.claude/hooks/memory-recall.py

# memory_search 단독 호출
python3 ~/.claude/scripts/mindvault/recall_cli.py "테스트" --source memory

# 인덱스 강제 재구축
python3 -c "from memory_indexer import full_rebuild; full_rebuild()"

# DB 상태
sqlite3 ~/.claude/mindvault-v3/index.db "SELECT COUNT(*) FROM memories; SELECT COUNT(*) FROM memories_vec;"

# 최근 hook 로그
tail -20 ~/.claude/mindvault-v3/debug.log | grep hook-recall
```

---

## 7. 테스트 전략

### 7.1 단위 테스트

**`tests/test_memory_indexer.py`**:
- frontmatter 파싱: 정상 / description 누락 / yaml malformed / frontmatter 없음
- mtime 비교: 변경 감지 / skip / 삭제 처리
- redact: secret 패턴 마스킹 (기존 indexer.py 재사용 검증)
- path traversal 차단: 심볼릭 링크, `..`, 절대경로 외부 → invalid
- BGE-M3 mock (HTTP fake → 1024 벡터 반환)

**`tests/test_memory_search.py`**:
- RRF 결합 수식 정확성: `1/(60+rank_vec) + 1/(60+rank_fts)`
- 한쪽만 hit (vec only / fts only) → 점수 정상
- 양쪽 모두 hit → 합산 점수 > 단일
- description 가중치 1.5x 적용
- threshold 게이트: 0.65 미만 필터링
- 빈 메모리/빈 인덱스 → 빈 결과
- 정규화 0~1 범위 보장

**`tests/test_memory_hook.py`**:
- 짧은 prompt (< 3자) → 빈 출력
- BGE-M3 mock timeout → silent exit 0
- BGE-M3 connection refused → silent exit 0
- 정상 경로: stdin JSON → stdout system-reminder 포맷
- malformed stdin → silent exit 0
- stderr 오염 없음 검증

**`tests/test_install_uninstall.py`** (light):
- install.sh이 settings.json UserPromptSubmit 배열에 정확히 1개 추가
- 기존 hook 보존 검증 (append, 덮어쓰기 X)
- uninstall.sh이 정확히 그 1개만 제거

### 7.2 통합 테스트

**선결조건**: BGE-M3 서버 기동. `MV3_SKIP_INTEGRATION=1`이면 skip.

- **E2E 1 — 기본 회수**: fixture md 3개 → full_rebuild → `recall_memory("메일 보내는 법")` → sendmail top-1, score > 0.65
- **E2E 2 — 한국어 활용형**: "메일 보냈더라" → 임베딩 hit
- **E2E 3 — 정확 식별자**: "msmtp" → FTS5 hit이 RRF 상위
- **E2E 4 — hook 성능**: 100회 호출 평균 < 100ms, p95 < 200ms
- **E2E 5 — 회귀**: Sprint 1/2/3 정상

### 7.3 수동 검증 (release gate)

배포 후 형이 직접 확인:
1. "예전에 그 폰트 깨졌던 거" → `project_hyperframes_fonts_fix` 회수
2. "고마"·"ㅇㅇ"·"잠깐" → 회수 안 뜸
3. 평균 메시지 응답 체감 지연 없음
4. `launchctl unload com.yonghaekim.bge-m3-mlx` → 에러 없이 회수만 빠짐
5. `/cs` → memory_review approve → 다음 세션 hook에서 새 메모리 회수
6. `/recall 메일` → sessions + memory 둘 다 결과 표시

### 7.4 성능 벤치마크

`tests/benchmark_search.py`:

| 지표 | 목표 |
|---|---|
| 임베딩 1회 (BGE-M3) | < 50ms |
| sqlite-vec top-10 | < 5ms |
| FTS5 top-10 | < 5ms |
| RRF 결합 + 정규화 | < 2ms |
| hook 전체 (콜드) | < 150ms |
| hook 전체 (웜) | < 80ms |
| full_rebuild (100 파일) | < 20초 |
| incremental (1 파일) | < 200ms |

미달 시: profile → 병목 식별. BGE-M3 양자화 더 작게 또는 lru_cache.

### 7.5 회귀 수동 트리거

```bash
cd ~/my-folder/apps/mindvault-v3
python3 -m unittest discover tests/
MV3_SKIP_INTEGRATION=0 python3 -m unittest tests.test_integration
python3 tests/benchmark_search.py
./install.sh --dry-run
```

---

## 8. 의존성·설치

### 8.1 Python 패키지

추가:
- `sqlite-vec` (≥0.1.0) — sqlite-vec 확장 모듈
- `mlx-embeddings` 또는 `mlx-lm` (BGE-M3 추론용)
- `pyyaml` (frontmatter 파싱, stdlib 대안: 자체 정규식 파서)

기존 유지:
- `httpx` 또는 stdlib `urllib.request` (HTTP 호출)
- `sqlite3` (stdlib)

### 8.2 시스템 의존성

- macOS (Apple Silicon 권장 — MLX 성능)
- Python 3.10+
- launchd (사용자 LaunchAgents 권한)
- 디스크 여유: 인덱스 DB ~50MB, BGE-M3 모델 ~400MB, sqlite-vec extension ~3MB

### 8.3 설치 흐름

```bash
cd ~/my-folder/apps/mindvault-v3
./install.sh
```

내부:
1. Python 의존성 설치
2. BGE-M3 모델 다운로드 (`huggingface_hub.snapshot_download('mlx-community/bge-m3-mlx-4bit', local_dir='~/.cache/mlx-bge-m3')`)
3. launchd plist 생성 + load
4. 헬스체크 (BGE-M3 응답)
5. UserPromptSubmit hook 등록 (settings.json append)
6. 초기 `full_rebuild()` 실행
7. 완료 메시지

---

## 9. 알려진 한계 / 미래 작업

### 9.1 한계

1. **BGE-M3 8192 토큰 cap** — 매우 긴 메모리 파일은 truncate. 현재 형 메모리는 평균 1.5~6KB라 영향 없음. 향후 한 파일이 8K 초과 시 자동 분할 정책 필요.
2. **한 파일 = 한 청크** — 한 파일에 `[YYYY-MM-DD]` 누적이 길어지면 회수 정밀도 저하. 임계 기준 (예: 5KB 초과)에서 자동 시간 단위 분할 정책 추가는 미래 작업.
3. **유사도 임계값 0.65는 휴리스틱** — 실 사용 hit rate 데이터 모인 뒤 조정 필요. `metrics.jsonl` 기반 분석으로 적정값 도출.
4. **MEMORY.md 인덱스 자체는 임베딩 안 함** — frontmatter description과 중복이라 의도적 제외. 향후 형이 인덱스에만 적는 메모(파일 없는 경우)가 생기면 추가 검토.
5. **WikiLink (`[[name]]`) 그래프 활용 X** — 회수된 메모리에 wikilink로 연결된 다른 메모리를 자동 끌어오는 graph expansion은 MVP 스코프 외.

### 9.2 미래 작업 후보 (Sprint 5+)

- **시간 단위 청킹**: 5KB 초과 파일에 한해 `[YYYY-MM-DD]` 단위 자동 분할.
- **WikiLink graph expansion**: top-k 결과의 `[[name]]` 링크를 따라 1-hop 확장.
- **JSONL 세션을 임베딩 인덱스에도 추가**: 현재 FTS5만. 메모리 자산이 충분히 활용되면 다음 단계.
- **다국어 쿼리 정규화**: "메일" vs "이메일" vs "email" 자동 확장 (BGE-M3가 어느 정도 자체 흡수하지만 한계 있음).
- **CLAUDE.md `[메모리 회수 Ritual]` 폐기**: 자동 hook이 충분히 안정되면 수동 회수 규칙 제거.
- **회수 결과 사용자 피드백 루프**: 형이 회수 결과를 "도움됨/안됨" 표시하면 임계값/가중치 자동 튜닝.

---

## 10. 변경 요약

| 카테고리 | 항목 |
|---|---|
| 신규 파일 | `src/memory_indexer.py`, `src/memory_search.py`, `~/.claude/hooks/memory-recall.py`, `tests/test_memory_*.py`, `~/Library/LaunchAgents/com.yonghaekim.bge-m3-mlx.plist` |
| 수정 파일 | `src/recall_cli.py` (확장), `src/memory_review_cli.py` (1줄 추가), `install.sh`, `uninstall.sh`, `README.md` |
| DB 스키마 | `memories`, `memories_fts`, `memories_vec` 추가. `schema_version` V1 → V2 |
| 인프라 | BGE-M3 launchd 서비스 (포트 8081). Gemma 서비스(8080) 패턴 복사 |
| settings.json | UserPromptSubmit 배열에 hook 1개 추가 |
| 의존성 | `sqlite-vec`, `mlx-embeddings` 추가 |
| 비파괴성 | 기존 Sprint 1/2/3 컴포넌트·DB 무손상. 순수 추가 |
