"""T-B2 / T-B1b / T-B5 / T-B6 — eval_gate (evaluate 양방향·baseline 구조·CLI exit).

두 그룹(plan §2.3):
- hermetic(항상 실행): evaluate()·baseline 구조·CLI exit — 임베딩/서버 불요, 결정적.
- integration(MV3_RUN_EVAL_GATE=1 + Arctic-ko 8081): 실 러너 smoke. 미가동 시 skip.
"""
from __future__ import annotations

import json
import os
import shutil
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

import eval_gate
from eval_gate import DEFAULT_THRESHOLDS, evaluate, main

REPO = Path(__file__).resolve().parent.parent

THR = {
    "min_recall_at_5": 0.85,
    "min_first_hit": 0.80,
    "max_recall_drop": 0.03,
    "max_mrr_drop": 0.03,
}


def _metrics(recall, first_hit=0.97, mrr=0.98):
    return {
        "mean_recall_at_5": recall,
        "mean_precision_at_5": 0.2,
        "mrr": mrr,
        "first_relevant_hit_rate": first_hit,
        "expected_top1_hit_rate": first_hit,
        "n": 32,
    }


# ════════════ HERMETIC ════════════
# ── T-B2 — evaluate() 양방향 ──────────────────────────────────────────
def test_evaluate_pass_equal():
    base = _metrics(1.0)
    v = evaluate(dict(base), dict(base), THR)
    assert v["verdict"] == "pass"
    assert v["breaches"] == []


def test_evaluate_fail_correctness():
    cur = _metrics(0.50)  # < min_recall_at_5 0.85
    v = evaluate(cur, _metrics(1.0), THR)
    assert v["verdict"] == "fail"
    assert any(b["kind"] == "correctness" and b["metric"] == "mean_recall_at_5" for b in v["breaches"])


def test_evaluate_fail_regression():
    # baseline 0.80 → current 0.74, drop 0.06 > max_recall_drop 0.03.
    # current 0.74 < min_recall 0.85 도 위반이지만, regression breach 가 반드시 잡혀야.
    thr = dict(THR, min_recall_at_5=0.70)  # correctness 통과시켜 regression 격리
    v = evaluate(_metrics(0.74), _metrics(0.80), thr)
    assert v["verdict"] == "fail"
    regs = [b for b in v["breaches"] if b["kind"] == "regression" and b["metric"] == "mean_recall_at_5"]
    assert regs and regs[0]["drop"] == pytest.approx(0.06)


def test_evaluate_pass_improve():
    # current > baseline → 개선은 절대 fail 아님
    v = evaluate(_metrics(1.0), _metrics(0.90), THR)
    assert v["verdict"] == "pass"


def test_evaluate_regression_at_tolerance_boundary_passes():
    """정확히 허용폭인 drop(float 오차로 경계 초과)은 pass. adversarial review R7.
    1.0-0.97=0.030000000000000027 > 0.03 (float) 인데 epsilon 으로 흡수돼 통과해야."""
    thr = dict(THR, min_recall_at_5=0.50)  # correctness 통과시켜 regression 격리
    assert (1.0 - 0.97) > 0.03  # float 오차 전제 확인
    v = evaluate(_metrics(0.97), _metrics(1.0), thr)
    assert v["verdict"] == "pass", f"at-tolerance drop should pass, got {v['breaches']}"
    # 허용폭 명백 초과(0.04)는 여전히 fail
    v2 = evaluate(_metrics(0.96), _metrics(1.0), thr)
    assert v2["verdict"] == "fail"
    assert any(b["kind"] == "regression" for b in v2["breaches"])


def test_evaluate_correctness_at_floor_passes():
    """정확히 floor(min_recall) 값은 통과(float 경계 흡수)."""
    v = evaluate(_metrics(0.85), _metrics(0.85), THR)  # min_recall_at_5=0.85
    assert v["verdict"] == "pass"


def test_evaluate_fail_closed_missing_metric():
    v = evaluate({}, _metrics(1.0), THR)
    assert v["verdict"] == "fail"
    assert any(b["kind"] == "fail_closed" for b in v["breaches"])


def test_evaluate_mrr_regression():
    v = evaluate(_metrics(1.0, mrr=0.90), _metrics(1.0, mrr=0.98), THR)
    assert v["verdict"] == "fail"
    assert any(b["kind"] == "regression" and b["metric"] == "mrr" for b in v["breaches"])


# ── 동적 k (--k≠5) — adversarial review 2026-06-17 ────────────────────
def test_evaluate_dynamic_k_pass():
    """--k≠5 에서 완벽 회수가 fail-closed 로 오판되지 않음(동적 키)."""
    cur = {"mean_recall_at_10": 1.0, "first_relevant_hit_rate": 1.0, "mrr": 1.0, "n": 10}
    base = {"mean_recall_at_10": 1.0, "first_relevant_hit_rate": 1.0, "mrr": 1.0, "n": 10}
    v = evaluate(cur, base, THR, k=10)
    assert v["verdict"] == "pass"


def test_evaluate_dynamic_k_missing_key_fail_closed():
    """k=10 인데 current 에 mean_recall_at_5 만 있으면(잘못된 키) fail-closed."""
    cur = {"mean_recall_at_5": 1.0, "first_relevant_hit_rate": 1.0, "mrr": 1.0}
    v = evaluate(cur, _metrics(1.0), THR, k=10)
    assert v["verdict"] == "fail"
    assert any(b["kind"] == "fail_closed" and b["metric"] == "mean_recall_at_10" for b in v["breaches"])


def test_evaluate_mrr_fail_closed_when_missing():
    """current 에 mrr 키 없으면 fail-closed(mr/fh 와 대칭)."""
    v = evaluate({"mean_recall_at_5": 1.0, "first_relevant_hit_rate": 1.0}, _metrics(1.0), THR)
    assert v["verdict"] == "fail"
    assert any(b["kind"] == "fail_closed" and b["metric"] == "mrr" for b in v["breaches"])


def test_cli_k10_non_json_prints_dynamic_key(tmp_path, capsys):
    """--k 10 비-json 출력이 크래시 없이 exit 0 + 실제 recall@10 값 출력(동적 키)."""
    bp = tmp_path / "b.json"
    bp.write_text(json.dumps({
        "schema_version": 1, "k": 10, "thresholds": THR,
        "metrics": {"mean_recall_at_10": 0.95, "first_relevant_hit_rate": 1.0, "mrr": 1.0, "n": 10},
        "per_query": [],
    }), encoding="utf-8")
    rep = {"skipped": False, "k": 10, "per_query": [], "metrics": {
        "mean_recall_at_10": 0.95, "mean_precision_at_10": 0.1, "mrr": 1.0,
        "first_relevant_hit_rate": 1.0, "expected_top1_hit_rate": 1.0, "n": 10}}
    with patch.object(eval_gate, "run_corpus", lambda qrels_path, **kw: rep):
        rc = main(["--qrels", "x.json", "--baseline", str(bp), "--k", "10"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "recall@10=0.950" in out  # 0.0 default 가 아니라 실제 값


def test_load_baseline_non_dict_root(tmp_path):
    """baseline JSON 루트가 비-dict(배열/스칼라)면 ValueError(AttributeError 크래시 아님)."""
    bp = tmp_path / "b.json"
    bp.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError, match="object"):
        eval_gate._load_baseline(bp)


def test_cli_non_dict_baseline_is_usage(tmp_path):
    """비-dict baseline → main 이 usage(exit 2)로 처리(unhandled crash 아님)."""
    bp = tmp_path / "b.json"
    bp.write_text('"just a string"', encoding="utf-8")
    with patch.object(eval_gate, "run_corpus", _mock_report(_metrics(1.0))):
        rc = main(["--qrels", "x.json", "--baseline", str(bp)])
    assert rc == 2


def test_load_baseline_non_dict_thresholds(tmp_path):
    """thresholds/metrics 가 비-dict 면 ValueError(AttributeError 크래시 아님). R5."""
    bp = tmp_path / "b.json"
    bp.write_text(json.dumps({"schema_version": 1, "thresholds": [1, 2], "metrics": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="thresholds"):
        eval_gate._load_baseline(bp)
    bp.write_text(json.dumps({"schema_version": 1, "thresholds": {}, "metrics": "bad"}), encoding="utf-8")
    with pytest.raises(ValueError, match="metrics"):
        eval_gate._load_baseline(bp)


def test_load_baseline_non_numeric_metric_value(tmp_path):
    """metrics/threshold 값이 비-숫자(문자열)면 ValueError — evaluate 산술 TypeError
    크래시(exit 계약 위반) 차단(codex 2-track R11)."""
    bp = tmp_path / "b.json"
    bp.write_text(json.dumps({
        "schema_version": 1, "thresholds": THR,
        "metrics": {"mean_recall_at_5": "1.0", "mrr": 0.9},
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="number"):
        eval_gate._load_baseline(bp)
    # threshold 값이 문자열인 경우도
    bp.write_text(json.dumps({
        "schema_version": 1, "thresholds": {"min_recall_at_5": "0.85"}, "metrics": {},
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="number"):
        eval_gate._load_baseline(bp)


def test_load_baseline_rejects_non_finite(tmp_path):
    """NaN/Infinity metric·threshold 거부 — 비교 silent 우회/회귀제한 무력화 차단(codex R12).
    json.loads 가 NaN/Infinity 리터럴을 float 로 파싱하므로 명시 거부 필요."""
    bp = tmp_path / "b.json"
    bp.write_text('{"schema_version": 1, "thresholds": {"max_recall_drop": Infinity}, "metrics": {}}',
                  encoding="utf-8")
    with pytest.raises(ValueError, match="finite"):
        eval_gate._load_baseline(bp)
    bp.write_text('{"schema_version": 1, "thresholds": {}, "metrics": {"mean_recall_at_5": NaN}}',
                  encoding="utf-8")
    with pytest.raises(ValueError, match="finite"):
        eval_gate._load_baseline(bp)


# ── use_ctx 정합 가드 (R5: CR-on baseline vs CR-off 평가 차단) ─────────
def test_write_baseline_records_use_ctx(tmp_path):
    report = {"metrics": _metrics(1.0), "use_ctx": False, "per_query": []}
    bp = tmp_path / "b.json"
    eval_gate._write_baseline(bp, report, dict(DEFAULT_THRESHOLDS), 5, "t")
    data = json.loads(bp.read_text(encoding="utf-8"))
    assert data["use_ctx"] is False


def test_cli_use_ctx_mismatch_is_usage(tmp_path):
    """baseline use_ctx=False 인데 평가가 use_ctx=True(CR-on) → usage(exit 2)."""
    bp = tmp_path / "b.json"
    bp.write_text(json.dumps({
        "schema_version": 1, "k": 5, "use_ctx": False, "thresholds": THR,
        "metrics": _metrics(1.0), "per_query": [],
    }), encoding="utf-8")
    rep = {"skipped": False, "k": 5, "use_ctx": True, "per_query": [], "metrics": _metrics(1.0)}
    with patch.object(eval_gate, "run_corpus", lambda qrels_path, **kw: rep):
        rc = main(["--qrels", "x.json", "--baseline", str(bp)])
    assert rc == 2


def test_cli_use_ctx_match_passes(tmp_path):
    """동일 use_ctx(False=False)면 정상 평가."""
    bp = tmp_path / "b.json"
    bp.write_text(json.dumps({
        "schema_version": 1, "k": 5, "use_ctx": False, "thresholds": THR,
        "metrics": _metrics(1.0), "per_query": [],
    }), encoding="utf-8")
    rep = {"skipped": False, "k": 5, "use_ctx": False, "per_query": [], "metrics": _metrics(1.0)}
    with patch.object(eval_gate, "run_corpus", lambda qrels_path, **kw: rep):
        rc = main(["--qrels", "x.json", "--baseline", str(bp)])
    assert rc == 0


def test_cli_k_mismatch_is_usage(tmp_path):
    """baseline k=5 인데 --k 10 으로 평가 → usage(exit 2). 회귀 게이트 무력화 차단."""
    bp = tmp_path / "b.json"
    bp.write_text(json.dumps({
        "schema_version": 1, "k": 5, "thresholds": THR, "metrics": _metrics(1.0), "per_query": [],
    }), encoding="utf-8")
    rep = {"skipped": False, "k": 10, "per_query": [], "metrics": {
        "mean_recall_at_10": 1.0, "mrr": 1.0, "first_relevant_hit_rate": 1.0, "n": 10}}
    with patch.object(eval_gate, "run_corpus", lambda qrels_path, **kw: rep):
        rc = main(["--qrels", "x.json", "--baseline", str(bp), "--k", "10"])
    assert rc == 2


# ── T-B1b part2 — baseline 파일 구조 ──────────────────────────────────
def test_write_baseline_structure(tmp_path):
    report = {
        "metrics": _metrics(1.0),
        "per_query": [{"query_id": "q1", "retrieved": ["m"], "recall_at_k": 1.0}],
    }
    bp = tmp_path / "b.json"
    eval_gate._write_baseline(bp, report, dict(DEFAULT_THRESHOLDS), 5, "test-label")
    data = json.loads(bp.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    for key in ("thresholds", "generated_at", "git_commit", "k", "metrics", "per_query", "label"):
        assert key in data, key
    for mk in ("mean_recall_at_5", "mean_precision_at_5", "mrr",
               "first_relevant_hit_rate", "expected_top1_hit_rate", "n"):
        assert mk in data["metrics"], mk
    assert data["per_query"][0]["query_id"] == "q1"
    # 재로드 가능
    reloaded = eval_gate._load_baseline(bp)
    assert reloaded["metrics"]["n"] == 32


# ── T-B6 — CLI exit 코드 (run_corpus mock 으로 hermetic) ───────────────
def _baseline_file(tmp_path, metrics, thresholds=THR):
    bp = tmp_path / "baseline.json"
    bp.write_text(json.dumps({
        "schema_version": 1, "thresholds": thresholds, "metrics": metrics, "per_query": [],
    }), encoding="utf-8")
    return bp


def _mock_report(metrics, skipped=False, reason=None):
    def _run(qrels_path, **kw):
        if skipped:
            return {"skipped": True, "reason": reason, "k": 5, "per_query": [], "metrics": {}}
        return {"skipped": False, "k": 5, "per_query": [], "metrics": metrics}
    return _run


def test_cli_exit_pass(tmp_path):
    bp = _baseline_file(tmp_path, _metrics(1.0))
    with patch.object(eval_gate, "run_corpus", _mock_report(_metrics(1.0))):
        rc = main(["--qrels", "x.json", "--baseline", str(bp)])
    assert rc == 0


def test_cli_exit_fail(tmp_path):
    bp = _baseline_file(tmp_path, _metrics(1.0))
    with patch.object(eval_gate, "run_corpus", _mock_report(_metrics(0.50))):
        rc = main(["--qrels", "x.json", "--baseline", str(bp)])
    assert rc == 1


def test_cli_exit_usage_bad_qrels(tmp_path):
    # run_corpus 미patch → 실제로 없는 qrels 로드 시도 → FileNotFoundError → exit 2
    bp = _baseline_file(tmp_path, _metrics(1.0))
    rc = main(["--qrels", str(tmp_path / "nope.json"), "--baseline", str(bp)])
    assert rc == 2


def test_cli_exit_skip_is_usage(tmp_path):
    bp = _baseline_file(tmp_path, _metrics(1.0))
    with patch.object(eval_gate, "run_corpus", _mock_report({}, skipped=True, reason="arctic down")):
        rc = main(["--qrels", "x.json", "--baseline", str(bp)])
    assert rc == 2


def test_cli_fail_closed_on_exception(tmp_path):
    bp = _baseline_file(tmp_path, _metrics(1.0))

    def _raise(qrels_path, **kw):
        raise RuntimeError("per-query boom")

    with patch.object(eval_gate, "run_corpus", _raise):
        rc = main(["--qrels", "x.json", "--baseline", str(bp)])
    assert rc == 1  # fail-closed, not crash


def test_cli_json_output(tmp_path, capsys):
    bp = _baseline_file(tmp_path, _metrics(1.0))
    with patch.object(eval_gate, "run_corpus", _mock_report(_metrics(1.0))):
        main(["--qrels", "x.json", "--baseline", str(bp), "--json"])
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["verdict"] == "pass"
    assert "current" in parsed


def test_cli_update_baseline(tmp_path):
    bp = tmp_path / "newbase.json"  # 없음 → 기본 임계로 생성
    report = {"skipped": False, "k": 5, "metrics": _metrics(1.0),
              "per_query": [{"query_id": "q1", "retrieved": ["m"], "recall_at_k": 1.0}]}
    with patch.object(eval_gate, "run_corpus", lambda qrels_path, **kw: report):
        rc = main(["--qrels", "x.json", "--baseline", str(bp), "--update-baseline"])
    assert rc == 0
    data = json.loads(bp.read_text(encoding="utf-8"))
    assert data["metrics"]["n"] == 32
    assert data["thresholds"]["min_recall_at_5"] == DEFAULT_THRESHOLDS["min_recall_at_5"]


# ════════════ INTEGRATION (MV3_RUN_EVAL_GATE=1 + Arctic-ko 8081) ════════════
def _arctic_up():
    try:
        with urllib.request.urlopen("http://localhost:8081/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


_RUN_INTEGRATION = os.environ.get("MV3_RUN_EVAL_GATE") == "1" and _arctic_up()
_skip_reason = "integration: set MV3_RUN_EVAL_GATE=1 and run Arctic-ko 8081"


@pytest.mark.skipif(not _RUN_INTEGRATION, reason=_skip_reason)
def test_integration_runner_real_arctic(tmp_path):
    """실 Arctic-ko 로 fixture 인덱스 회수 — skip 아님, 메트릭 산출(B4/B5)."""
    from memory_indexer import incremental_index
    from eval_runner import run_corpus

    fixture_src = REPO / "tests" / "fixtures" / "memory"
    mem_dir = tmp_path / "memory"
    shutil.copytree(fixture_src, mem_dir)
    db = tmp_path / "idx.db"
    incremental_index([mem_dir], db_path=db)  # 실 Arctic 임베딩

    qrels = tmp_path / "q.json"
    qrels.write_text(json.dumps({
        "schema_version": 1,
        "queries": [
            {"query_id": "f1", "query": "메일 발송 SMTP 설정", "relevant": ["test-mail"], "expected_top1": "test-mail"},
            {"query_id": "f2", "query": "스캐너 자동 크롭", "relevant": ["test-scanner"], "expected_top1": "test-scanner"},
        ],
    }), encoding="utf-8")
    report = run_corpus(qrels, db_path=db, k=5, require_arctic=True)
    assert report["skipped"] is False
    assert report["metrics"]["n"] == 2
    assert "mean_recall_at_5" in report["metrics"]
