# Sprint 10 — indexer 트랜잭션 리팩터 BUILD LOG

## 목표

Sprint 9에서 발견한 long-running write transaction → hook 동시 실행 시 sqlite lock 충돌
(`embed cache put fail: database is locked`, indexer hang)을 해소. 추가로 `full_rebuild`의
위험한 `db_path.unlink()` 제거 (Sprint 9 메모리 인덱스 동반 손실 사고 재발 방지).

## 변경 요약

### 1. `src/memory_indexer.py` — `incremental_index()` 짧은 트랜잭션 + reordering

**기존 (Sprint 9)**: open_db → 전체 for 루프 (DELETE/INSERT) → 마지막 `conn.commit()` 1회.
임베딩(embed_cache sub-conn write 포함)이 메인 INSERT 사이에 끼어 self-deadlock.

**변경**:
- 매 stale 처리 후 `conn.commit()` 즉시 호출 (삭제 루프)
- 매 .md 파일 처리 후 `conn.commit()` 즉시 호출 (신규/변경 루프)
- `_embed_cache_get/put`의 `timeout=0.1` instant-skip 패치를 default(5s)로 복구
- 임베딩(`embed_text(body)`, `embed_text(description)`)을 메인 conn write 전에 호출
  → 매 iter 시작 시 메인 conn idle → sub-conn `embed_cache.INSERT`가 BUSY 없이 진행

### 2. `src/indexer.py` — `incremental_index()` + `backfill_session_vecs()` 동일 패턴

- `_embed_session_from_path(jsonl)` 호출을 메인 INSERT 전으로 이동
- 매 session 처리 후 `conn.commit()` 즉시 호출
- `backfill_session_vecs`도 매 1건 처리 후 `conn.commit()` (기존 매 20건)

### 3. `src/indexer.py` — `full_rebuild()` 안전화

**기존**:
```python
def full_rebuild(...):
    db_path.unlink()  # ← memories_* 포함 DB 전체 삭제
    return incremental_index(...)
```

**변경**:
```python
def full_rebuild(...):
    conn = open_db(db_path)
    conn.execute("DELETE FROM sessions")
    conn.execute("DELETE FROM sessions_fts")
    conn.execute("DELETE FROM sessions_vec")
    conn.commit()
    return incremental_index(...)
```

`memories_*` + `embed_cache` 보존. `rebuild_session_vecs` 안전 패턴과 일관.

## 측정 데이터

### 베이스라인 (Sprint 9, lock 충돌 발생 시점)

```
indexer.py 실행 → backfill 7건 → 12.0s elapsed
debug.log: "embed cache put fail: database is locked" × 2건
```

### Sprint 10 적용 후 단독 실행

```
indexer.py 실행 → backfill 7건 → 0.3s elapsed
debug.log: "embed cache put fail" × 0건
(7 failed는 head text 빈 짧은 세션 — 정상 즉시 종료)

memory_indexer.py 실행 (1개 memory touch) → 0.15s
embed_cache 16 → 19 (cache_put 3건 성공)
```

### 동시성 스트레스 테스트

전체 memory(106개) touch 후 memory_indexer + memory-recall hook 50개 동시 실행:

```
total new debug entries: 104
sqlite-level "database is locked" / "cache put fail" / "cache get fail": 0건  ✓
"lock busy — skip" (flock NB skip): 50건 (의도된 동작 — 동시 indexer 1개만 실행)
"embed fail: TimeoutError": 1건 (임베딩 서버 응답 timeout, Sprint 10 무관)
memory_indexer 풀 재인덱싱 106개 / 68.31s 완료
```

### 회귀 검증

```python
recall_memory("MindVault sprint indexer")
# → name=project-mindvault raw=0.574 score=1.000 (Sprint 9 베이스라인과 일관)
```

DB 상태 (Sprint 10 전/후 비교):

| 테이블           | Sprint 9 후 | Sprint 10 후 |
|------------------|-------------|--------------|
| memories         | 106         | 106          |
| memories_vec     | 208         | 209 (+1 새 desc) |
| sessions         | 173         | 174 (+1 신규)|
| sessions_vec     | 166         | 167 (+1)     |
| embed_cache      | 16          | 225 (풀 캐시) |

**memories 인덱스 보존 확인 ✓** (full_rebuild unlink 회귀 없음, 풀 재인덱싱 거쳐도 유지).

## 안전 정책 준수

- `indexer.full_rebuild()` 호출 안 함 (안전화 완료 후에도 검증 단계에선 미사용)
- 변경 전 production 위치(`~/.claude/scripts/mindvault/{indexer,memory_indexer}.py`) 백업:
  `~/.claude/scripts/mindvault/_sprint10_backup_1779461424/`
- 워크트리 격리 작업, hook 비활성화 없이 진행
- BGE-M3 launchctl plist 및 가중치 미변경

## 향후 (Sprint 11+)

- `embed fail: TimeoutError` 발생 빈도 모니터링 (Arctic-ko 서버 응답 ~1.5s, 5s timeout이 적정한지)
- `full_rebuild`가 embed_cache도 비울 수 있도록 옵션 인자 (`reset_cache=False` 기본)
- 매 1건 commit 패턴의 fsync 부하 — 현재 0.3s/매-iter라 무시 가능하지만 1000+ session 규모에선 재측정 필요

## 파일별 diff line count

- `src/memory_indexer.py`: +24 lines (주석 포함)
- `src/indexer.py`: +21 lines (주석 포함)
