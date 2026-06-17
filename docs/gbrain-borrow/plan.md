# plan.md — 구현 계획

> 짝: [goal.md](./goal.md) · [status.md](./status.md) · [test.md](./test.md)
> 모든 touchpoint는 검증된 `file:line`. 신규 파일은 `(NEW)` 표기.

## 0. 시퀀싱 원칙

**Feature B(Eval Gate)를 먼저** 구축한다. 이유: B는 A의 측정 계기다. B가 없으면 A4("CR이 `off` 대비 비열등")를 증명할 수 없다. 순서:

```
Phase 0  설계 동결 (이 문서) + 워크트리
Phase 1  [B] qrels 코퍼스 + 스코어러 (P@k/R@k/MRR)        ← 측정 계기
Phase 2  [B] baseline 게이트 + CLI + pytest 배선          ← off 모드 baseline 확정
Phase 3  [A] 스키마 v4 마이그레이션 (신컬럼, off=무영향)
Phase 4  [A] 인덱서 CR 생성 (title/synopsis, Gemma, 폴백)
Phase 5  [A] 검색 CR 경로 (모드 플래그, contextual 임베딩 사용)
Phase 6  [A] CR 백필 CLI (corpus_generation stale 재임베딩)
Phase 7  [A×B] A/B 측정 — synopsis vs off, 게이트로 A4 판정
Phase 8  문서화·정리·머지 결정
```

각 Phase는 **기존 816 pytest green 유지**가 통과 조건. TDD: 실패 테스트 → 구현 → green.

---

## Phase 1 — [B] 라벨 코퍼스 + 스코어러

### 1.1 qrels 코퍼스 `evals/recall_qrels.json` (NEW)
gbrain `qrels-file.ts` 스키마를 MV `name` 단일 slug로 축약:

```json
{
  "schema_version": 1,
  "_description": "MV recall regression corpus — query→expected memory name(s)",
  "queries": [
    {
      "query_id": "q01-sendmail",
      "query": "메일 보내는 sendmail SMTP 설정 어떻게 했지",
      "relevant": ["sendmail-cli"],
      "expected_top1": "sendmail-cli",
      "label": "tool-recall",
      "source": "handlabeled"
    }
  ]
}
```
- `relevant`: 정답 메모리 `name`(frontmatter name, = `memories.name`) 배열. ≥1.
- `expected_top1`: top-1으로 와야 할 name(선택). 없으면 expected_top1 메트릭 제외.
- `label`: 도메인 카테고리(tool-recall / project / procedural / decision / coined-name …).
- `source`: `handlabeled` | `metrics_mined`(출처 태깅, R3 완화).
- 시드: ① `src/eval_top3_domain.py:28-39`의 기존 10쿼리를 라벨로 승격(세션 대상이라 메모리용으로 재작성), ② `metrics.jsonl`의 `recalled_ids`에서 picked>0 쿼리 후보 추출 → **사람 확인 후** 채택. 합계 ≥20.

### 1.2 스코어러 `src/ranking_metrics.py` (NEW)
순수 함수, 임베딩/IO 0, 결정적:

```python
def precision_at_k(retrieved: list[str], relevant: set[str], k: int) -> float
def recall_at_k(retrieved: list[str], relevant: set[str], k: int) -> float
def first_relevant_hit(retrieved: list[str], relevant: set[str]) -> int   # retrieved[0] in relevant
def expected_top1_hit(retrieved: list[str], expected_top1: str|None) -> int|None
def reciprocal_rank(retrieved: list[str], relevant: set[str]) -> float     # 1/rank, 0 if none
def score_corpus(per_query: list[dict]) -> dict   # mean_p@k, mean_r@k, mrr, first_hit_rate, expected_top1_rate, n
```
- gbrain `correctness-gate.ts:100-112` / `qrels-file.ts:201-230` 공식 이식. recall@k = |retrieved[:k] ∩ relevant| / |relevant|.
- `retrieved`는 `name` 순위 리스트. `recall_memory` 결과 dict의 `name` 필드(`src/memory_search.py` 반환 키)에서 추출.

### 1.3 러너 `src/eval_runner.py` (NEW)
```python
def run_corpus(qrels_path, db_path=None, k=5, raw_cosine_min=DEFAULT_RAW_COSINE_MIN) -> dict:
    # 각 query → recall_memory(query, top_k=k, score_threshold=0, raw_cosine_min=...) 직접 호출
    #   (hook/intent 우회 → 결정성 B5; score_threshold=0 으로 순위 전체 평가)
    # → per_query: {query_id, retrieved:[name...], latency_ms, recall@k, p@k, rr, ...}
    # → score_corpus 집계 + 리포트 dict 반환
```
- **중요**: `recall_memory`를 `top_k=k`(예 5)로 호출 — 운영 기본 `DEFAULT_TOP_K=1`과 분리. eval은 순위 품질을 봐야 하므로 k≥5.
- `score_threshold=0` 으로 게이트 미적용 순위를 평가(품질 자체 측정). raw_cosine 게이트는 운영값 유지(현실 반영)하되 파라미터화.
- Arctic-ko 8081 미가동 → `recall_memory`가 vec 없이 FTS-only로 동작하거나 예외; 러너는 감지해 **skip 사유 리턴**(B4 graceful).

### 1.4 테스트 `tests/test_ranking_metrics.py` (NEW)
- 순수함수 단위테스트(알려진 입력→기대값). recall@k 경계(k> len, relevant 다중, 빈 retrieved). → test.md T-B1.

---

## Phase 2 — [B] baseline 게이트 + CLI + 배선

### 2.1 baseline 파일 `evals/recall_baseline.json` (NEW, 생성물)
gbrain baseline-metadata 패턴 축약(NDJSON 대신 단일 JSON — MV 규모 작음):
```json
{
  "schema_version": 1,
  "label": "off-mode v3.8.5 baseline",
  "generated_at": "<stamp>",
  "git_commit": "a1a979e",
  "k": 5,
  "thresholds": { "min_recall_at_5": 0.70, "min_first_hit": 0.60, "max_recall_drop": 0.03, "max_mrr_drop": 0.03 },
  "metrics": { "mean_recall_at_5": 0.0, "mean_precision_at_5": 0.0, "mrr": 0.0, "first_relevant_hit_rate": 0.0, "expected_top1_hit_rate": 0.0, "n": 0 },
  "per_query": [ { "query_id": "q01-sendmail", "retrieved": ["sendmail-cli"], "recall_at_5": 1.0 } ]
}
```
- **절대 임계**(correctness) + **회귀 임계**(baseline 대비 drop 허용폭) 둘 다. gbrain dual-gate 이식.
- `git_commit`·`generated_at`은 **스크립트가 런타임에 스탬프**(워크플로 스크립트는 Date.now 금지지만 이 CLI는 일반 파이썬이라 OK).

### 2.2 게이트 `src/eval_gate.py` (NEW)
```python
def evaluate(current: dict, baseline: dict, thresholds: dict) -> dict:
    # breaches = []
    # correctness: current.mean_recall_at_5 < thr.min_recall_at_5 → breach … (first_hit 등)
    # regression: (baseline.mean_recall_at_5 - current) > thr.max_recall_drop → breach (mrr 동일)
    # return {"verdict": "pass"|"fail", "breaches":[{metric,observed,threshold}], ...}

def main():  # argparse
    # --qrels evals/recall_qrels.json --baseline evals/recall_baseline.json [--k 5] [--json] [--update-baseline]
    # run_corpus → evaluate → 리포트 출력
    # exit 0 pass / 1 fail(또는 예외) / 2 usage  ← gbrain exit 규약
    # --update-baseline: 현재 metrics를 baseline에 기록(사람이 의도적으로 갱신할 때만)
```
- fail-closed: query별 예외는 게이트 fail(gbrain D3).
- `--update-baseline`은 명시 플래그로만 — 무심코 회귀를 baseline에 굳히는 사고 방지.

### 2.3 pytest 배선 `tests/test_eval_gate.py` (NEW)
- **두 그룹 분리** (스코프 모순 방지):
  - **hermetic 그룹(항상 실행)**: (a) 합성 코퍼스+baseline로 `evaluate()` pass/fail 양방향, qrels 스키마 검증. 임베딩 불요·순수·결정적(B3 핵심) → 마커/skip 없이 일반 816 run에 포함.
  - **integration 그룹**: (b) 실제 러너 smoke. `@pytest.mark.slow`(또는 env `MV3_RUN_EVAL_GATE=1`)로 일반 run에서 분리, **Arctic-ko 8081 미가동 시 이 그룹만 `pytest.skip`**.
- 즉 file-level skip 금지 — skip은 integration 그룹에만 적용(hermetic은 불침해).

### 2.4 baseline 확정
Arctic-ko 가동 상태에서 `python -m src.eval_gate --update-baseline` 1회 → `off` 모드 현 성능을 baseline에 고정. 이게 Phase 7 A/B의 기준선.

### 2.5 (선택) 문서/실행 진입점
`evals/README.md` (NEW): 코퍼스 추가법·게이트 실행법·baseline 갱신 정책. CI 없는 MV에 맞춘 "수동/pre-push" 운영 안내. (GitHub Actions 신설 금지 — 비범위.)

---

## Phase 3 — [A] 스키마 v4 마이그레이션

### 3.1 `src/indexer.py` 변경
- `SCHEMA_VERSION = 3 → 4` (`:53`).
- `_migrate_schema` (`:274`)에 추가:
```python
if current < 4:
    conn.execute("ALTER TABLE memories_vec ADD COLUMN embedding_ctx BLOB")       # contextual 임베딩(nullable)
    conn.execute("ALTER TABLE memories_vec ADD COLUMN cr_synopsis TEXT")          # 생성된 synopsis(감사/재현)
    conn.execute("ALTER TABLE memories ADD COLUMN cr_mode TEXT")                  # 이 메모리가 임베딩된 tier
    conn.execute("ALTER TABLE memories ADD COLUMN corpus_generation TEXT")        # 16-char 해시, stale 감지
    current = 4
```
- 신규 `CREATE TABLE memories_vec` DDL(`:247-253`)에도 동일 컬럼 추가(신규 DB 일관).
- **off 무영향**: 신컬럼 전부 nullable, 검색은 모드 off일 때 `embedding`만 사용 → 기존 동작 불변(A1).

### 3.2 테스트 `tests/test_schema_v2.py` 확장(기존 파일) / `tests/test_migration_v4.py` (NEW)
- v3→v4 마이그레이션 멱등·신컬럼 존재·기존 데이터 보존. → test.md T-A1.

---

## Phase 4 — [A] 인덱서 CR 생성

### 4.1 모드/상수 `src/memory_indexer.py`
```python
CR_MODES = ("off", "title", "synopsis")
CR_MODE = os.environ.get("MV3_CR_MODE", "off")     # 기본 off
SYNOPSIS_PROMPT_VERSION = 1
WRAPPER_VERSION = 1
GEMMA_SYNOPSIS_TIMEOUT = 8.0                         # index-time, 관대 (회수 경로 아님)
```

### 4.2 wrapper/synopsis 헬퍼 (gbrain `embedding-context.ts`·`page-summary.ts` 이식)
```python
def _sanitize_ctx(s: str, cap: int = 300) -> str:        # </context> strip, 공백축약, cap
def build_contextual_prefix(name: str, synopsis: str|None) -> str|None:
    # <context>{name}\n{synopsis}</context>\n  (synopsis 없으면 title-only)
def wrap_body_for_embedding(body: str, prefix: str|None) -> str:   # prefix+body, prefix None이면 body
def generate_synopsis_gemma(name, description, body) -> tuple[str|None, str]:
    # 로컬 Gemma 8080 호출(enable_thinking=False). 1문장 15~30단어.
    # 성공 → (synopsis, "ok"); 거부/빈/타임아웃/다운 → (None, reason)  ← 분류 후 폴백
def compute_corpus_generation(mode) -> str:
    # sha256(f"{mode}|{SYNOPSIS_PROMPT_VERSION}|{GEMMA_MODEL}|{WRAPPER_VERSION}|arctic-ko-v2")[:16]
```
- Gemma 프롬프트는 gbrain `page-summary.ts:55-69` system + XML user 이식 → 한국어 메모리용으로 조정(`gemma-worker` 규약 재사용 가능). 출력 캡 ~64토큰.

### 4.3 인덱싱 루프 통합 (`src/memory_indexer.py:443-498`)
```python
vec_body = embed_text(body) if body.strip() else None         # 기존 raw — 항상 유지
vec_desc = embed_text(description) if description.strip() else None   # 기존 — 불변

embedding_ctx = None; cr_synopsis = None; effective_mode = "off"
if CR_MODE != "off" and body.strip():
    synopsis = None
    if CR_MODE == "synopsis":
        synopsis, reason = generate_synopsis_gemma(name, description, body)
        effective_mode = "synopsis" if synopsis else "title"   # 강등(R2)
    else:
        effective_mode = "title"
    # title tier는 description 을 맥락으로 사용(LLM 0)
    ctx_line = synopsis or description or None
    prefix = build_contextual_prefix(name, ctx_line)
    if prefix:
        wrapped = wrap_body_for_embedding(body, prefix)
        embedding_ctx = embed_text(wrapped)                    # 실패 시 None → raw 폴백
        cr_synopsis = ctx_line
    if embedding_ctx is None:
        effective_mode = "off"                                 # 완전 폴백, 인덱싱 무중단

# 저장: 기존 INSERT 에 신컬럼 추가
# memories_vec(path, kind='body', embedding, embedding_ctx, cr_synopsis)
# memories SET cr_mode=effective_mode, corpus_generation=compute_corpus_generation(effective_mode)
```
- **원본 불변**: `embedding`(raw), `memories_fts.body`, 스니펫 경로 전부 그대로. `embedding_ctx`만 추가.
- **defer 패턴 계승**: `vec_body`/`vec_desc` 실패는 기존대로 파일 skip. `embedding_ctx` 실패는 **off 폴백**(파일 skip 아님 — raw로 정상 인덱싱).
- description 임베딩엔 CR 미적용(이미 synopsis 성격).

### 4.4 테스트 `tests/test_cr_indexer.py` (NEW)
- title 모드: prefix 형태, embedding_ctx 채워짐, off와 raw embedding 동일.
- synopsis 모드: Gemma mock(성공/거부/타임아웃) → 강등·폴백 경로. → test.md T-A2.

---

## Phase 5 — [A] 검색 CR 경로

### 5.1 `src/memory_search.py`
- `_vec_top_k` (`:291-360`)에 모드 인지: `MV3_CR_SEARCH` 또는 인자로 `use_ctx: bool`.
  - off: 현행 `embedding` 사용(불변).
  - on: body 행은 `COALESCE(embedding_ctx, embedding)` 사용(ctx 없으면 raw 폴백). description 행은 항상 `embedding`.
- 쿼리 임베딩은 **wrapper 미적용 clean**(asymmetric, gbrain Note 4) — 변경 없음.
- RRF/게이트/정렬 하류 로직 불변. raw_cosine 게이트는 사용한 임베딩의 코사인 기준(ctx면 ctx 코사인).
- **회수 query-time 추가비용 0**: synopsis 생성은 인덱싱에서 끝남. 검색은 컬럼 선택만 바뀜(A3).

### 5.2 모드 기본값
- 운영 회수 hook은 당분간 `off`(A1). 검색 CR은 **eval/실험 경로에서만 on**(env). 머지 후 A4 통과 시 별도 결정으로 기본 전환(goal §5 A4).

### 5.3 테스트 `tests/test_cr_search.py` (NEW)
- off: 기존 `test_memory_search` 회귀 0.
- on: embedding_ctx 있는 메모리가 선택되는 fixture, ctx null이면 raw 폴백. → test.md T-A3.

---

## Phase 6 — [A] CR 백필 CLI

### 6.1 `src/backfill_cli.py` 확장 또는 `src/cr_backfill_cli.py` (NEW)
- `--cr-backfill [--mode title|synopsis] [--limit N] [--dry-run]`
- `corpus_generation`이 현재 `compute_corpus_generation(mode)`와 다른(또는 NULL) 메모리만 재임베딩 → 멱등·재개 가능(중단 후 재실행 안전).
- Gemma rate 보호: synopsis 모드는 메모리당 1회 + 관대한 sleep. 대량은 배치.
- `memory_indexer.full_rebuild`(`:510`)와의 관계 명시: full_rebuild는 raw만, cr-backfill은 ctx 채움.

### 6.2 테스트 `tests/test_cr_backfill.py` (NEW)
- stale 감지(generation 불일치만 재처리), dry-run, 재개. → test.md T-A4.

---

## Phase 7 — [A×B] A/B 측정

1. Arctic-ko 가동, `off` baseline 확정(Phase 2.4).
2. `MV3_CR_MODE=synopsis` 백필 → `MV3_CR_SEARCH=1` 로 `python -m src.eval_gate --qrels … --baseline …(off)` 실행.
3. 판정(goal A4): synopsis가 `off` baseline 대비 P@5·R@5 **비열등(drop ≤ 허용폭)** + coined-name 케이스 ≥1 개선이면 **A 채택**, 아니면 **기본 off 유지 + 사유 status.md 기록**.
4. title vs synopsis도 비교(무료 title이 충분하면 Gemma 비용 회피).

---

## Phase 8 — 문서/정리/머지

- status.md 최종 수렴 기록, test.md 결과 채움.
- 머지 조건: 816 + 신규 테스트 green, A1(off 회귀 0), B3 게이트 양방향, D1/D2 충족.
- CLAUDE.md "sqlite-vec" 부정확 표기 교정 PR 별도 메모(범위 밖이나 발견 기록).

---

## 영향 파일 요약

| 파일 | 종류 | Phase | 비고 |
|---|---|---|---|
| `evals/recall_qrels.json` | NEW | 1 | 라벨 코퍼스 ≥20 |
| `src/ranking_metrics.py` | NEW | 1 | 순수 P@k/R@k/MRR |
| `src/eval_runner.py` | NEW | 1 | recall_memory 직접호출 |
| `src/eval_gate.py` | NEW | 2 | dual-gate, exit 0/1/2 |
| `evals/recall_baseline.json` | NEW(생성) | 2 | off baseline |
| `evals/README.md` | NEW | 2 | 운영 안내 |
| `tests/test_ranking_metrics.py` | NEW | 1 | |
| `tests/test_eval_gate.py` | NEW | 2 | slow 마커 |
| `src/indexer.py` | EDIT `:53,:247,:274` | 3 | 스키마 v4 |
| `tests/test_migration_v4.py` | NEW | 3 | |
| `src/memory_indexer.py` | EDIT `:5,:443-498` | 4 | CR 생성·폴백 |
| `tests/test_cr_indexer.py` | NEW | 4 | Gemma mock |
| `src/memory_search.py` | EDIT `:291-360` | 5 | ctx 컬럼 선택 |
| `tests/test_cr_search.py` | NEW | 5 | |
| `src/cr_backfill_cli.py` | NEW | 6 | stale 재임베딩 |
| `tests/test_cr_backfill.py` | NEW | 6 | |

## 비결정·미해결 (status.md가 추적)
- Q1. title-only(무료)가 충분히 좋으면 synopsis(Gemma) 불필요할 수 있음 → Phase 7 데이터로 결정.
- Q2. 장문 메모리 절단(8192토큰)은 CR이 못 고침 → 청킹 후속(비범위).
- Q3. 운영 hook 검색을 CR on으로 기본 전환할지는 A4 통과 후 **별도 사용자 결정**(자동 전환 금지).
