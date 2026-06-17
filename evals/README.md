# MV recall eval gate

gbrain `correctness-gate`/`qrels-file` 패턴을 MV 규모로 이식한 **회수 회귀 게이트**.
검색 변경(특히 Contextual Retrieval)이 회수 정확도를 떨어뜨렸는지 머지 전 자동 판정한다.
MV는 `.github` 없는 **로컬 전용** — CI(GitHub Actions) 강제 안 함. 게이트 = pytest +
`python -m src.eval_gate` CLI(+선택 pre-push). 외부 서비스 의존 0.

## 구성

| 파일 | 역할 |
|---|---|
| `recall_qrels.json` | 라벨 코퍼스 — `query → relevant 메모리 name`. 32 쿼리, 도메인 분산(tool/coined-name/procedural/project/decision/user/reference + paraphrase). |
| `recall_baseline.json` | off-mode 기준선 — metrics + 임계(thresholds) + git_commit/생성시각. 회귀 비교 기준. |
| `src/ranking_metrics.py` | 순수 P@k/R@k/MRR/first_hit/expected_top1 (임베딩·IO 0). |
| `src/eval_runner.py` | qrels 로드(스키마검증) + `recall_memory` **직접 호출**(hook/intent 우회 → 결정성). |
| `src/eval_gate.py` | dual-gate(correctness 절대임계 + regression baseline drop) + CLI(exit 0/1/2). |

## 실행

```bash
# 게이트 평가 (Arctic-ko 8081 필요). 운영 인덱스 대상:
MV3_DATA_DIR=~/.claude/mindvault-v3 python3 -m src.eval_gate \
  --qrels evals/recall_qrels.json --baseline evals/recall_baseline.json --json

# 종료코드: 0 pass / 1 fail(회귀·correctness·예외 fail-closed) / 2 usage(잘못된 인자·측정불가)

# pytest (hermetic 그룹은 항상, integration 그룹은 아래 env 로):
python3 -m pytest tests/test_ranking_metrics.py tests/test_eval_runner.py tests/test_eval_gate.py -q
MV3_RUN_EVAL_GATE=1 python3 -m pytest tests/test_eval_gate.py -q -k integration   # Arctic-ko 8081 가동 시
```

## baseline 갱신 정책

`--update-baseline` 은 **명시 플래그로만** — 무심코 회귀를 baseline 에 굳히는 사고 방지.
검색 알고리즘을 의도적으로 바꿔 새 성능을 기준으로 삼을 때만 사람이 실행한다.

```bash
MV3_DATA_DIR=~/.claude/mindvault-v3 python3 -m src.eval_gate \
  --qrels evals/recall_qrels.json --baseline evals/recall_baseline.json \
  --update-baseline --label "off-mode <버전> baseline"
```

## 코퍼스 추가법

`recall_qrels.json` 의 `queries` 에 항목 추가:
- `query_id`(고유), `query`(자연어), `relevant`(정답 메모리 `name` 배열, ≥1), 선택 `expected_top1`/`label`/`source`.
- `relevant` name 은 **실제 인덱싱된 `memories.name`**(frontmatter name)과 일치해야 함.
- 시드 출처 태깅(`source`): `handlabeled` | `metrics_mined`. 후자는 `metrics.jsonl` 의
  `recalled_ids` 후보 → **사람 확인 후** 채택(과적합 회피).
- 추가 후 baseline 재생성(`--update-baseline`) 필요(쿼리 수·기준 변동).

## 임계(thresholds) 의미 — `recall_baseline.json`

- `min_recall_at_5` / `min_first_hit` : **correctness** 절대 하한(baseline 무관 최소 품질).
- `max_recall_drop` / `max_mrr_drop` : **regression** — baseline 대비 허용 하락폭. 초과 시 fail.

> off-mode 기준선(2026-06-17, 261 메모리 prod 인덱스): recall@5=1.0, first_hit=0.969,
> mrr=0.984. MV는 사람이 쓴 frontmatter `description` 을 별도 1.5x 벡터로 임베딩해
> 이미 강한 맥락 신호를 가져 baseline 이 거의 천장 — 따라서 게이트는 주로 **회귀 보호**
> 역할이며, CR 의 한계효용은 gbrain(raw chunk만 임베딩) 대비 낮다(Phase 7 A/B 참조).
