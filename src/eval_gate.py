#!/usr/bin/env python3
"""MindVault v3 — eval 게이트 (plan §2.2, gbrain dual-gate 이식).

라벨 코퍼스 회수 메트릭을 (1) 절대 임계(correctness) + (2) baseline 회귀로 판정.
gbrain exit 규약: 0 pass / 1 fail / 2 usage(또는 측정 불가). fail-closed —
측정 못 하면 pass 아님.

CLI:
    python -m src.eval_gate --qrels evals/recall_qrels.json \
        --baseline evals/recall_baseline.json [--k 5] [--json] [--update-baseline]
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from eval_runner import DEFAULT_RAW_COSINE_MIN, run_corpus  # noqa: E402

DEFAULT_QRELS = "evals/recall_qrels.json"
DEFAULT_BASELINE = "evals/recall_baseline.json"
BASELINE_SCHEMA_VERSION = 1

# baseline 파일이 없을 때 쓰는 기본 임계(off-mode 측정 후 --update-baseline 으로 고정).
DEFAULT_THRESHOLDS = {
    "min_recall_at_5": 0.85,
    "min_first_hit": 0.80,
    "max_recall_drop": 0.03,
    "max_mrr_drop": 0.03,
}

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_USAGE = 2

# float 뺄셈/비교 경계 오차 흡수(IEEE-754 배정밀 오차 ≫ 1e-9 ≫ 유의미 메트릭 델타).
# "정확히 허용폭" drop·"정확히 floor" 값이 가짜 fail 나는 것 방지(adversarial review R7).
FP_EPS = 1e-9


def evaluate(current: dict, baseline: dict, thresholds: dict, k: int = 5) -> dict:
    """현재 metrics 를 절대 임계 + baseline 회귀로 판정.

    current / baseline 은 score_corpus 출력 형태(mean_recall_at_{k}, mrr,
    first_relevant_hit_rate ...). recall 키는 k 에 따라 동적(score_corpus 와 일치).
    핵심 지표(recall/first_hit/mrr) 누락 → fail-closed breach.
    반환: {"verdict": "pass"|"fail", "breaches": [{kind,metric,...}]}
    """
    breaches: list[dict] = []
    recall_key = f"mean_recall_at_{k}"  # score_corpus 동적 키와 일치(--k≠5 호환)
    # 임계 키도 k-aware(동적 메트릭 키와 정합), 없으면 legacy min_recall_at_5 폴백.
    recall_thr = thresholds.get(f"min_recall_at_{k}", thresholds.get("min_recall_at_5"))

    mr = current.get(recall_key)
    fh = current.get("first_relevant_hit_rate")
    mrr = current.get("mrr")

    # ── correctness (절대 임계) — 핵심 지표 누락 시 fail-closed ──
    if mr is None:
        breaches.append({"kind": "fail_closed", "metric": recall_key,
                         "observed": None, "threshold": recall_thr})
    else:
        if recall_thr is not None and mr < recall_thr - FP_EPS:  # floor: 정확히 동일은 통과
            breaches.append({"kind": "correctness", "metric": recall_key,
                             "observed": mr, "threshold": recall_thr})
    if fh is None:
        breaches.append({"kind": "fail_closed", "metric": "first_relevant_hit_rate",
                         "observed": None, "threshold": thresholds.get("min_first_hit")})
    else:
        thr = thresholds.get("min_first_hit")
        if thr is not None and fh < thr - FP_EPS:
            breaches.append({"kind": "correctness", "metric": "first_relevant_hit_rate",
                             "observed": fh, "threshold": thr})
    # mrr 도 mr/fh 와 대칭으로 fail-closed (지표 누락 시 조용히 pass 방지)
    if mrr is None:
        breaches.append({"kind": "fail_closed", "metric": "mrr",
                         "observed": None, "threshold": thresholds.get("max_mrr_drop")})

    # ── regression (baseline 대비 drop) ──
    # FP_EPS: float 뺄셈 오차로 *정확히 허용폭*인 drop 이 경계를 넘겨(예: 1.0-0.97=
    # 0.030000000000000027 > 0.03) 가짜 regression fail 나는 것 방지(adversarial review R7).
    # 1e-9 는 IEEE-754 배정밀 오차보다 훨씬 크고 유의미한 메트릭 델타보다 훨씬 작다.
    b_mr = baseline.get(recall_key)
    max_rdrop = thresholds.get("max_recall_drop")
    if b_mr is not None and mr is not None and max_rdrop is not None:
        drop = b_mr - mr
        if drop > max_rdrop + FP_EPS:
            breaches.append({"kind": "regression", "metric": recall_key,
                             "observed": mr, "baseline": b_mr, "drop": drop, "threshold": max_rdrop})
    b_mrr = baseline.get("mrr")
    max_mdrop = thresholds.get("max_mrr_drop")
    if b_mrr is not None and mrr is not None and max_mdrop is not None:
        drop = b_mrr - mrr
        if drop > max_mdrop + FP_EPS:
            breaches.append({"kind": "regression", "metric": "mrr",
                             "observed": mrr, "baseline": b_mrr, "drop": drop, "threshold": max_mdrop})

    return {"verdict": "fail" if breaches else "pass", "breaches": breaches}


def _git_commit() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None


def _load_baseline(path: Path) -> dict:
    """baseline 파일 로드. 없으면 기본 임계만 가진 빈 baseline 반환."""
    if not path.is_file():
        return {"schema_version": BASELINE_SCHEMA_VERSION, "thresholds": dict(DEFAULT_THRESHOLDS),
                "metrics": {}, "per_query": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"baseline invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("baseline root must be an object")
    if data.get("schema_version") != BASELINE_SCHEMA_VERSION:
        raise ValueError(f"baseline schema_version must be {BASELINE_SCHEMA_VERSION}")
    # sub-field 타입 검증 — 비-dict thresholds/metrics 가 .get() 에서 AttributeError
    # 크래시하지 않고 clean ValueError→EXIT_USAGE 로 가도록(adversarial review R5).
    if "thresholds" in data and not isinstance(data["thresholds"], dict):
        raise ValueError("baseline thresholds must be an object")
    if "metrics" in data and not isinstance(data["metrics"], dict):
        raise ValueError("baseline metrics must be an object")
    # 값 타입 검증 — (1) 문자열 등 비-숫자가 evaluate 의 `b - m` 산술에서 미캐치 TypeError
    # 크래시(exit 0/1/2 계약 위반), (2) NaN/Infinity 가 모든 비교를 False 로 만들어 게이트
    # breach 를 silent 우회(NaN) / 회귀제한 무력화(Inf) 하는 것 차단(codex 2-track R11/R12).
    # json.loads 는 기본 parse_constant 로 NaN/Infinity 리터럴을 float 로 파싱하므로 명시 거부.
    for field in ("thresholds", "metrics"):
        sub = data.get(field)
        if isinstance(sub, dict):
            for key, val in sub.items():
                if val is None:
                    continue
                if isinstance(val, bool) or not isinstance(val, (int, float)):
                    raise ValueError(
                        f"baseline {field}.{key} must be a number, got {type(val).__name__}")
                if isinstance(val, float) and not math.isfinite(val):
                    raise ValueError(f"baseline {field}.{key} must be finite, got {val}")
    return data


def _write_baseline(path: Path, report: dict, thresholds: dict, k: int, label: str) -> None:
    """현재 측정치를 baseline 파일로 기록(--update-baseline). per_query 는 회귀
    원인 추적용으로 query_id+retrieved+recall 만 슬림하게 저장."""
    slim = [
        {"query_id": q["query_id"], "retrieved": q["retrieved"],
         f"recall_at_{k}": q["recall_at_k"]}
        for q in report.get("per_query", [])
    ]
    out = {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "label": label,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_commit": _git_commit(),
        "k": k,
        "use_ctx": report.get("use_ctx"),  # 회수 모드 기록(CR on/off) — 비교 정합 가드용
        "thresholds": thresholds,
        "metrics": report["metrics"],
        "per_query": slim,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="MV recall eval gate (dual: correctness + regression).")
    ap.add_argument("--qrels", default=DEFAULT_QRELS)
    ap.add_argument("--baseline", default=DEFAULT_BASELINE)
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--raw-cosine-min", type=float, default=DEFAULT_RAW_COSINE_MIN)
    ap.add_argument("--json", action="store_true", help="결과를 JSON 으로 출력")
    ap.add_argument("--update-baseline", action="store_true",
                    help="현재 측정치를 baseline 에 기록(의도적 갱신 시에만)")
    ap.add_argument("--label", default="off-mode baseline")
    args = ap.parse_args(argv)

    qrels_path = Path(args.qrels)
    baseline_path = Path(args.baseline)

    # ── 측정 ──
    try:
        report = run_corpus(qrels_path, k=args.k, raw_cosine_min=args.raw_cosine_min)
    except FileNotFoundError as e:
        print(f"usage error: {e}", file=sys.stderr)
        return EXIT_USAGE
    except ValueError as e:
        print(f"usage error: invalid qrels — {e}", file=sys.stderr)
        return EXIT_USAGE
    except Exception as e:  # fail-closed: 측정 중 예외 → fail
        print(f"gate FAIL — eval 실행 예외: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_FAIL

    if report.get("skipped"):
        # 측정 불가(Arctic 미가동 등) — pass 아님. 환경 미비 → usage.
        msg = {"verdict": "skip", "reason": report.get("reason")}
        print(json.dumps(msg, ensure_ascii=False) if args.json
              else f"gate SKIP — {report.get('reason')}", file=sys.stderr)
        return EXIT_USAGE

    try:
        baseline = _load_baseline(baseline_path)
    except ValueError as e:
        print(f"usage error: {e}", file=sys.stderr)
        return EXIT_USAGE
    thresholds = baseline.get("thresholds") or dict(DEFAULT_THRESHOLDS)

    # ── baseline 갱신 모드 ──
    if args.update_baseline:
        _write_baseline(baseline_path, report, thresholds, args.k, args.label)
        print(f"baseline updated → {baseline_path}  metrics={report['metrics']}")
        return EXIT_PASS

    # ── k 정합 가드: baseline 이 다른 k 로 굳혀졌으면 회귀 비교가 무의미 → usage ──
    base_k = baseline.get("k")
    if baseline.get("metrics") and base_k is not None and base_k != args.k:
        print(f"usage error: --k {args.k} 가 baseline k({base_k})와 불일치 — "
              f"같은 k 로 평가하거나 --update-baseline 로 재고정하세요", file=sys.stderr)
        return EXIT_USAGE
    # ── use_ctx 정합 가드: CR-on baseline vs CR-off 평가(또는 반대)는 사과↔오렌지 → usage ──
    base_ctx = baseline.get("use_ctx")
    if baseline.get("metrics") and base_ctx is not None and base_ctx != report.get("use_ctx"):
        print(f"usage error: 평가 use_ctx({report.get('use_ctx')})가 baseline "
              f"use_ctx({base_ctx})와 불일치(MV3_CR_SEARCH 모드 차이) — 같은 모드로 "
              f"평가하거나 --update-baseline 로 재고정하세요", file=sys.stderr)
        return EXIT_USAGE

    # ── 판정 ──
    verdict = evaluate(report["metrics"], baseline.get("metrics", {}), thresholds, k=args.k)
    out = {
        "verdict": verdict["verdict"],
        "breaches": verdict["breaches"],
        "current": report["metrics"],
        "baseline": baseline.get("metrics", {}),
        "thresholds": thresholds,
        "k": args.k,
    }
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        m = report["metrics"]
        recall_key = f"mean_recall_at_{args.k}"
        print(f"[{verdict['verdict'].upper()}] recall@{args.k}={m.get(recall_key, 0.0):.3f} "
              f"first_hit={m['first_relevant_hit_rate']:.3f} mrr={m['mrr']:.3f} n={m['n']}")
        for b in verdict["breaches"]:
            print(f"  breach[{b['kind']}] {b['metric']}: observed={b.get('observed')} "
                  f"threshold={b.get('threshold')}"
                  + (f" baseline={b['baseline']} drop={b['drop']:.4f}" if b["kind"] == "regression" else ""))
    return EXIT_PASS if verdict["verdict"] == "pass" else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
