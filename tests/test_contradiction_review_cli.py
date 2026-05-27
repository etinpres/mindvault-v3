"""T6 review CLI tests (list / show only; resolve apply is T7's job)."""
from __future__ import annotations
import json
import pytest


def _write_queue(tmp_path, items):
    p = tmp_path / "contradictions.jsonl"
    p.write_text(
        "\n".join(json.dumps(d, ensure_ascii=False) for d in items) + "\n",
        encoding="utf-8",
    )
    return p


def test_list_unresolved_only(tmp_path, monkeypatch, capsys):
    from src import contradiction_review_cli as cli
    monkeypatch.setenv("MV3_RUNTIME_DIR", str(tmp_path))
    _write_queue(tmp_path, [
        {"new_slug": "a", "target_name": "b", "kind": "metric_update",
         "reason": "r1", "confidence": 0.9, "resolved": False},
        {"new_slug": "c", "target_name": "d", "kind": "fact_correction",
         "reason": "r2", "confidence": 0.8, "resolved": "dismissed"},
    ])
    rc = cli.main(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "[1]" in out
    assert "a" in out and "b" in out
    # resolved entries hidden
    assert "[2]" not in out
    # resolved entry's new_slug "c" must not appear in list output
    # (bare "c" check would collide with kind names containing 'c' like
    # "metric_update"/"decision_reversal"/"fact_correction", so check
    # specifically for the slug context "new=c" / "old=d")
    assert "new=c" not in out
    assert "old=d" not in out


def test_list_empty_runtime_dir(tmp_path, monkeypatch, capsys):
    from src import contradiction_review_cli as cli
    monkeypatch.setenv("MV3_RUNTIME_DIR", str(tmp_path))
    rc = cli.main(["list"])
    assert rc == 0
    assert "없음" in capsys.readouterr().out


def test_list_empty_jsonl_file(tmp_path, monkeypatch, capsys):
    """contradictions.jsonl exists but contains zero unresolved entries."""
    from src import contradiction_review_cli as cli
    monkeypatch.setenv("MV3_RUNTIME_DIR", str(tmp_path))
    _write_queue(tmp_path, [
        {"new_slug": "x", "target_name": "y", "kind": "metric_update",
         "reason": "r", "confidence": 0.9, "resolved": "dismissed"},
    ])
    rc = cli.main(["list"])
    assert rc == 0
    assert "없음" in capsys.readouterr().out


def test_list_skips_malformed_lines(tmp_path, monkeypatch, capsys):
    from src import contradiction_review_cli as cli
    monkeypatch.setenv("MV3_RUNTIME_DIR", str(tmp_path))
    p = tmp_path / "contradictions.jsonl"
    p.write_text(
        json.dumps({"new_slug": "good", "target_name": "x",
                    "kind": "metric_update", "reason": "r",
                    "confidence": 0.9, "resolved": False}) + "\n"
        + "not-json\n"
        + json.dumps({"new_slug": "good2", "target_name": "y",
                      "kind": "fact_correction", "reason": "r2",
                      "confidence": 0.85, "resolved": False}) + "\n",
        encoding="utf-8",
    )
    rc = cli.main(["list"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "good" in out
    assert "good2" in out
    assert "[1]" in out and "[2]" in out
    # No crash on malformed line


def test_show_renders_full_detail(tmp_path, monkeypatch, capsys):
    from src import contradiction_review_cli as cli
    monkeypatch.setenv("MV3_RUNTIME_DIR", str(tmp_path))
    _write_queue(tmp_path, [{
        "new_slug": "a", "new_path": "/x/a.md",
        "target_name": "b", "target_path": "/x/b.md",
        "kind": "metric_update", "reason": "65→66",
        "confidence": 0.9,
        "new_excerpt": "new body here",
        "old_excerpt": "old body here",
        "resolved": False,
    }])
    rc = cli.main(["show", "1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "new body here" in out
    assert "old body here" in out
    assert "65→66" in out
    assert "metric_update" in out
    assert "/x/a.md" in out
    assert "/x/b.md" in out


def test_show_out_of_range(tmp_path, monkeypatch, capsys):
    from src import contradiction_review_cli as cli
    monkeypatch.setenv("MV3_RUNTIME_DIR", str(tmp_path))
    _write_queue(tmp_path, [{
        "new_slug": "a", "target_name": "b",
        "kind": "metric_update", "reason": "r",
        "confidence": 0.9, "resolved": False,
    }])
    rc = cli.main(["show", "99"])
    assert rc == 1  # error exit


def test_resolve_dry_run_does_not_mutate(tmp_path, monkeypatch, capsys):
    """T6 resolve without --apply is dry-run only. No file/queue mutation."""
    from src import contradiction_review_cli as cli
    monkeypatch.setenv("MV3_RUNTIME_DIR", str(tmp_path))
    qp = _write_queue(tmp_path, [{
        "new_slug": "a", "target_name": "b",
        "kind": "metric_update", "reason": "r",
        "confidence": 0.9, "resolved": False,
    }])
    original = qp.read_text(encoding="utf-8")

    rc = cli.main(["resolve", "1", "--action", "dismiss"])  # no --apply
    assert rc == 0

    out = capsys.readouterr().out
    assert "dry-run" in out
    # jsonl unchanged
    assert qp.read_text(encoding="utf-8") == original


def test_list_survives_partial_key_rows(tmp_path, monkeypatch, capsys):
    """Defensive: rows missing some keys must not crash the entire list."""
    from src import contradiction_review_cli as cli
    monkeypatch.setenv("MV3_RUNTIME_DIR", str(tmp_path))
    p = tmp_path / "contradictions.jsonl"
    p.write_text(
        # Valid full row
        json.dumps({"new_slug": "good", "target_name": "a",
                    "kind": "metric_update", "reason": "r1",
                    "confidence": 0.9, "resolved": False}) + "\n"
        # Partial row missing reason and confidence — should not crash
        + json.dumps({"new_slug": "partial", "target_name": "b",
                      "kind": "fact_correction", "resolved": False}) + "\n"
        # Another good row
        + json.dumps({"new_slug": "good2", "target_name": "c",
                      "kind": "decision_reversal", "reason": "r3",
                      "confidence": 0.85, "resolved": False}) + "\n",
        encoding="utf-8",
    )
    rc = cli.main(["list"])
    assert rc == 0, "must not crash on partial-key row"
    out = capsys.readouterr().out
    assert "good" in out
    assert "partial" in out
    assert "good2" in out
    assert "[3]" in out


def test_load_all_is_public_helper(tmp_path, monkeypatch):
    """T7 needs to call load_all() to read full queue (including resolved)."""
    from src import contradiction_review_cli as cli
    monkeypatch.setenv("MV3_RUNTIME_DIR", str(tmp_path))

    # Verify public function exists (not underscore-prefixed)
    assert hasattr(cli, "load_all"), "load_all must be public for T7 reuse"
    assert callable(cli.load_all)

    p = tmp_path / "contradictions.jsonl"
    p.write_text(
        json.dumps({"new_slug": "x", "target_name": "y", "kind": "metric_update",
                    "reason": "r", "confidence": 0.9, "resolved": False}) + "\n"
        + json.dumps({"new_slug": "a", "target_name": "b", "kind": "fact_correction",
                      "reason": "r2", "confidence": 0.8, "resolved": "dismissed"}) + "\n",
        encoding="utf-8",
    )

    all_rows = cli.load_all()
    # Returns BOTH resolved and unresolved
    assert len(all_rows) == 2
    slugs = [d["new_slug"] for d in all_rows]
    assert "x" in slugs
    assert "a" in slugs
