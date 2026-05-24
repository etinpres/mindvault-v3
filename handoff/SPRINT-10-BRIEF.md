---
name: handoff-sprint10-brief
description: Sprint 10 brief — incremental_index long-running write transaction을 매-파일 commit 단위로 쪼개 hook(memory-recall, session-memory-precompute, session-memory-end) 동시 실행 시 sqlite lock 충돌 해소. Sprint 9 BGE-M3 → Arctic-Embed-L v2.0 KO dragonkue snowflake 4bit 교체 직후 잔여 작업
---

MindVault v3 Sprint 10 — indexer 트랜잭션 리팩터 + 잔여 정리

## 컨텍스트 (Sprint 9에서 넘어옴)

Sprint 9에서 BGE-M3 → Arctic-Embed-L v2.0 KO (dragonkue/snowflake-arctic-embed-l-v2.0-ko, MLX 4bit, port 8081) swap 완료. A/B 측정 결과 잡담 cosine 분포가 BGE-M3 0.67 → Arctic-ko 0.28로 떨어졌고 도메인 cosine 0.69 → 0.53. RELEVANT-NOISE gap이 +0.023 → +0.259로 11배 분리. raw cosine 게이트 0.78/0.62/0.58 → 0.40/0.32/0.30 으로 비례 재튜닝. 메모리/세션 인덱스 모두 풀 리빌드 완료.

## 발견한 근본 문제 — Sprint 10 본 작업

`src/memory_indexer.py`의 `incremental_index()` 및 `src/indexer.py`의 같은 함수가 **long-running write transaction** 패턴. 한 번의 `open_db()` → 모든 INSERT → 마지막 `conn.commit()`. 이게 hook들과 자주 충돌:

- 매 사용자 메시지마다 `~/.claude/hooks/memory-recall.py` (UserPromptSubmit) sqlite 접근
- Claude 응답 끝마다 `~/.claude/hooks/session-memory-precompute.sh` (Stop) → `~/.claude/scripts/mindvault/indexer.py` 백그라운드 detached 실행
- `~/.claude/hooks/session-memory-end.py` (SessionEnd) 도 sqlite write
- 게다가 `_embed_cache_put`이 sub-connection으로 sqlite write 시도 → 메인 conn의 long transaction과 자기-자신 deadlock 

Sprint 9 swap 중 인덱스 풀 리빌드가 lock 충돌로 30분 가까이 hang했음. 해결 방법으로 hook을 일시 비활성(rename .disabled)하고 진행. 그러나 운영 중에는 hook을 끌 수 없음.

## Sprint 10 목표

1. **`incremental_index()`를 매-파일/세션 commit으로 리팩터**
   - for 루프 안에서 N개(예: 10개)마다 `conn.commit()` → 트랜잭션 길이 최소화
   - hook과 동시 실행 시 lock 충돌 회피
   - sqlite WAL 모드 (이미 활성)에서 single-writer 제약 안에서 짧은 트랜잭션이 정답
   - `src/memory_indexer.py` + `src/indexer.py` 둘 다

2. **`_embed_cache_get/put` sub-connection 패턴 재검토**
   - 현재 임시 패치: Sprint 9에서 `sqlite3.connect(str(DB_PATH), timeout=0.1)` 으로 instant skip (자기-자신 deadlock 회피)
   - 진짜 fix: 메인 indexer 트랜잭션이 짧아지면 sub-conn 충돌 자체가 사라짐. timeout을 default(5초)로 되돌리거나, 또는 cache_put을 메인 conn 안에서 처리하도록 묶기
   - `src/memory_indexer.py:79, 101` (`sqlite3.connect(...timeout=0.1)` 두 줄)

3. **`indexer.full_rebuild()` DB unlink 위험 제거**
   - 현재 `db_path.unlink()` → DB 통째 삭제 후 incremental_index. Sprint 9에서 memory 인덱스까지 같이 날아간 사고 있었음
   - sessions_* 만 truncate하는 안전한 함수로 재작성
   - `src/indexer.py:441-448`

4. **선택사항 — hook 충돌 모니터링**
   - debug.log에 "database is locked" 또는 "embed cache put fail" 메시지 카운트
   - 운영 중 lock 충돌 빈도 추적 (Sprint 11 데이터 근거)

## 안전 정책 (Sprint 9 사고 교훈 반영)

- **`indexer.full_rebuild` 호출 금지** (DB unlink). 안전한 함수 새로 만들기 전엔 사용 금지.
- **memory 인덱스를 통째 truncate 하지 말 것**. Sprint 9 사고 재발 방지.
- 작업 중 hook 비활성이 필요하면 rename `.disabled` → 작업 후 무조건 복구. finally 블록 또는 `trap EXIT` 사용 권고.
- worktree 격리: 이 background 세션은 EnterWorktree 호출해서 isolation 보장.
- BGE-M3 launchctl plist와 가중치는 보존 (롤백 경로). 건드리지 말 것.

## 진행 환경

- 작업 디렉토리: `/Users/yonghaekim/my-folder/apps/mindvault-v3`
- 현재 운영 임베딩: Arctic-ko MLX 4bit, port 8081, launchctl `com.yonghaekim.arctic-ko-mlx`
- DB: `~/.claude/mindvault-v3/index.db` (WAL 모드 활성, synchronous=NORMAL)
- 현재 인덱스: memories 106, sessions 173, sessions_vec 166 (7개 vec 누락은 빈 head 추정 — 영향 없음)
- 한국어 응답, 토큰 절약 룰 적용 (CLAUDE.md 참조)
- Three Man Team 사용 가능: Arch / Bob / Richard

## 검증

리팩터 후:
1. hook 활성 상태에서 `python3 src/indexer.py` 직접 호출 → "database is locked" 0건 + 정상 완료
2. 매 형 메시지가 들어와도 indexer가 lock 충돌 없이 진행
3. `memory_search.recall_memory("MindVault sprint")` 정확 hit 유지 (raw 0.55 근처)
4. 풀 리빌드 시 메모리 인덱스 그대로 보존 확인 (DB unlink 회귀 없음)

## 산출물

- `handoff/SPRINT-10-BUILD-LOG.md` — 변경 요약, 측정 데이터, 회귀 검증 결과
- 코드 변경: `src/memory_indexer.py`, `src/indexer.py`
- 커밋 메시지: `feat(sprint10): short transactions in incremental_index — hook/indexer 동시성 lock 충돌 해소`
