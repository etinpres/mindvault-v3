# MindVault v2

Claude Code 세션 간 기억 유지 시스템. **4-layer 파이프라인**으로 세션 요약 자동 주입(SessionStart), FTS5+Gemma 풀텍스트 검색(`/recall`), SessionEnd staging+승인, 그리고 매 메시지마다 발동하는 임베딩+FTS5 hybrid 회수 hook까지. 로컬 Gemma + Arctic-Embed-L v2.0 KO (MLX 4bit) 서버라 API 비용 0원. Sprint 9에서 한국어 도메인 분리 11배 향상.

## Status

| Layer | 상태 | 기능 |
|---|---|---|
| 1. SessionStart 자동 주입 | ✅ 배포 | 최근 5세션 Gemma 요약 자동 주입. 캐시 히트 ~50ms |
| 2. /recall 검색 | ✅ 배포 | JSONL FTS5 + Gemma 재순위/요약 (sessions), memory hybrid RRF (memory) |
| 3. SessionEnd staging + /memory review | ✅ 배포 | 트리거 감지 → staged → 사용자 승인 → memory/*.md + reindex |
| 4. UserPromptSubmit hook (Sprint 4) | ✅ 배포 | memory/*.md hybrid 검색을 매 메시지 자동 주입 (silent fail, ~150ms p95) |

4-layer 완성 (2026-05-22).

## 요구사항

- macOS (Linux 미검증)
- Python 3.10+
- Claude Code
- 로컬 Gemma MLX 서버가 `http://localhost:8080`에서 실행 중이어야 함
  - `com.yonghaekim.gemma-mlx` launchd 서비스 권장
  - 모델: `mlx-community/gemma-4-e4b-it-4bit`
- **(Sprint 4+9)** 로컬 Arctic-ko MLX 서버가 `http://localhost:8081`에서 실행 중이어야 함
  - `com.yonghaekim.arctic-ko-mlx` launchd 서비스 (install.sh가 자동 설치)
  - 모델: `dragonkue/snowflake-arctic-embed-l-v2.0-ko` 원본을 MLX 4bit 양자화한 로컬 모델 (`~/.cache/mlx-arctic-ko/`)
  - **수동 변환 1회 필요** (mlx-community에 4bit 양자화본 미존재) — 아래 'Arctic-ko 모델 변환' 참조
  - 의존성: `pip install sqlite-vec mlx-embeddings pyyaml numpy huggingface_hub`

## 설치

```bash
./install.sh
```

설치 내용:
- `~/.claude/hooks/session-memory.py` 로 훅 스크립트 복사
- `~/.claude/settings.json`의 `SessionStart` 배열에 훅 등록 (기존 훅 보존, `settings.json.bak` 자동 백업)
- `~/.claude/mindvault-v2/cache/` 캐시 폴더 생성

설치 확인: 새 `claude` 세션을 열면 시스템 리마인더에 `# 지난 세션 요약 (MindVault v2)` 블록이 나타난다.

### Arctic-ko 모델 변환 (Sprint 9, 1회만 필요)

`install.sh` 가 launchd plist + 서버 스크립트 + 인덱스는 자동 배포하지만, **MLX 4bit 양자화 모델 자체**는 사용자가 직접 변환해야 합니다 (mlx-community 에 4bit 양자화본이 아직 없음).

```bash
# 의존성
pip install --user mlx_embeddings huggingface_hub

# 변환 (모델 ~1.1GB 다운로드 후 ~322MB 4bit 양자화, 총 1~2분)
python3 -c "from mlx_embeddings.utils import convert; convert('dragonkue/snowflake-arctic-embed-l-v2.0-ko', mlx_path='$HOME/.cache/mlx-arctic-ko', quantize=True, q_bits=4)"

# 결과 확인
ls ~/.cache/mlx-arctic-ko/model.safetensors

# install.sh 재실행 (또는 launchctl로 직접 로드)
./install.sh
```

변환된 모델은 `~/.cache/mlx-arctic-ko/` 에 영구 저장됩니다. 모델 자체는 한 번만 변환하면 됩니다 — 향후 install.sh 재실행 시 자동으로 skip.

**왜 수동인가**: Snowflake Arctic Embed L v2.0 한국어 fine-tune(dragonkue) 의 4bit MLX 변환본이 아직 mlx-community 에 업로드돼 있지 않음. 변환은 1회용이고 결과가 안정적이라 자동화는 다음 sprint 로 미룸.

## 제거

```bash
./uninstall.sh
```

훅 등록과 스크립트 파일만 제거. 캐시는 수동으로 지워야 함: `rm -rf ~/.claude/mindvault-v2`.

## 테스트

```bash
cd apps/mindvault-v2
python3 -m unittest tests.test_session_memory
```

21개 단위 테스트. 네트워크/Gemma 불필요.

## 동작 방식

```
[새 Claude 세션 시작]
        ↓
SessionStart 훅 실행 (session-memory.py)
        ↓
최근 5개 JSONL 찾기 (현재 세션 제외)
        ↓
파일 mtime 해시 → 캐시 확인
   ├── 캐시 HIT (~50ms) → 즉시 주입
   └── 캐시 MISS
        ↓
   JSONL 파싱 (user/assistant 텍스트만, 첫 6턴 + 마지막 6턴)
        ↓
   PII 패턴 마스킹 (sk-*, ghp_*, Bearer …)
        ↓
   Gemma 호출 (localhost:8080, 45초 타임아웃)
        ↓
   캐시 저장 + 컨텍스트 주입
```

에러(Gemma 다운, JSONL 없음, 파싱 실패)는 모두 조용히 패스하며 `exit 0`. 절대 세션 시작을 블로킹하지 않는다.

## 설정

`src/session_memory.py` 상수 섹션에서:

| 상수 | 기본값 | 설명 |
|---|---|---|
| `MAX_SESSIONS` | 5 | 요약 대상 세션 수 |
| `MAX_HEAD_TURNS` | 6 | 각 세션 앞쪽 턴 수 |
| `MAX_TAIL_TURNS` | 6 | 각 세션 뒤쪽 턴 수 |
| `MAX_MSG_CHARS` | 200 | 각 메시지 최대 글자 |
| `GEMMA_MAX_TOKENS` | 2000 | Gemma 응답 토큰 한도 (reasoning 포함) |
| `GEMMA_TIMEOUT` | 45 | Gemma 호출 타임아웃(초) |
| `CACHE_DAYS` | 7 | 캐시 보존 기간 |

## 제약 및 알려진 한계

1. **Gemma 4 E4B는 reasoning 모델** — 내부 사고에 토큰 많이 소비, max_tokens 크게 잡아야 함
2. **한국어 특화** — 프롬프트가 한국어로 최적화됨
3. **PII 필터는 키 패턴만** — 이메일/전화는 로컬 전용이라 통과
4. **세션 경계 = JSONL 파일** — 한 파일 안에서 주제 바뀌어도 하나로 간주

## 디버깅

- 훅 실행 로그: `~/.claude/mindvault-v2/debug.log` (hook-recall, mem-indexer, mem-search prefix)
- 캐시: `~/.claude/mindvault-v2/cache/*.txt`
- Sprint 1 수동 실행: `echo '{"sessionId":"현재세션ID"}' | python3 ~/.claude/hooks/session-memory.py`
- Sprint 4 hook 수동 실행: `echo '{"prompt":"테스트 쿼리"}' | python3 ~/.claude/hooks/memory-recall.py`
- Arctic-ko 헬스체크: `curl http://localhost:8081/health`  # {"ok":true,"model":"arctic-ko-mlx-4bit","dim":1024}
- Arctic-ko 재기동: `launchctl kickstart -k gui/$(id -u)/com.yonghaekim.arctic-ko-mlx`
- Arctic-ko 로그: `tail ~/Library/Logs/arctic-ko-mlx.{log,err}`
- 메모리 인덱스 강제 재구축: `python3 -c "import sys; sys.path.insert(0, '/Users/yonghaekim/.claude/scripts/mindvault'); from memory_indexer import full_rebuild; full_rebuild()"`
- DB 상태: `python3 -c "import sqlite3; c=sqlite3.connect('/Users/yonghaekim/.claude/mindvault-v2/index.db'); [print(t, c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]) for t in ('sessions','memories','memories_fts','memories_vec')]"`

## 라이선스

MIT
