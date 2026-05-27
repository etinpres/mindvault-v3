"""T6 review CLI tests (list / show only; resolve apply is T7's job)."""
from __future__ import annotations
import json
import re
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


def test_resolve_dismiss_apply_marks_jsonl(tmp_path, monkeypatch):
    from src import contradiction_review_cli as cli
    monkeypatch.setenv("MV3_RUNTIME_DIR", str(tmp_path))
    p = _write_queue(tmp_path, [
        {"new_slug": "a", "target_name": "b", "target_path": "/x/b.md",
         "kind": "fact_correction", "reason": "r", "confidence": 0.8,
         "resolved": False},
    ])

    rc = cli.main(["resolve", "1", "--action", "dismiss", "--apply"])
    assert rc == 0
    line = json.loads(p.read_text(encoding="utf-8").strip())
    assert line["resolved"] == "dismissed"


def test_resolve_supersede_apply_writes_frontmatter(tmp_path, monkeypatch):
    from src import contradiction_review_cli as cli
    monkeypatch.setenv("MV3_RUNTIME_DIR", str(tmp_path))

    mem = tmp_path / "memory"
    mem.mkdir()
    new_p = mem / "new_x.md"
    old_p = mem / "old_x.md"
    new_p.write_text(
        "---\nname: new-x\ntype: feedback\n---\n\nnew body\n",
        encoding="utf-8",
    )
    old_p.write_text(
        "---\nname: old-x\ntype: feedback\n---\n\nold body\n",
        encoding="utf-8",
    )

    _write_queue(tmp_path, [{
        "new_slug": "new-x", "new_path": str(new_p),
        "target_name": "old-x", "target_path": str(old_p),
        "kind": "decision_reversal", "reason": "r", "confidence": 0.9,
        "resolved": False,
    }])

    rc = cli.main(["resolve", "1", "--action", "supersede", "--apply"])
    assert rc == 0
    new_content = new_p.read_text(encoding="utf-8")
    old_content = old_p.read_text(encoding="utf-8")
    assert "supersedes:" in new_content and "old-x" in new_content
    assert "deprecated_by:" in old_content and "new-x" in old_content


def test_resolve_supersede_idempotent(tmp_path, monkeypatch):
    """Calling supersede twice does not duplicate the list entry."""
    from src import contradiction_review_cli as cli
    monkeypatch.setenv("MV3_RUNTIME_DIR", str(tmp_path))

    mem = tmp_path / "memory"
    mem.mkdir()
    new_p = mem / "new_x.md"
    old_p = mem / "old_x.md"
    new_p.write_text(
        "---\nname: new-x\nsupersedes: [old-x]\ntype: feedback\n---\n\nbody\n",
        encoding="utf-8",
    )
    old_p.write_text(
        "---\nname: old-x\ntype: feedback\n---\n\nbody\n",
        encoding="utf-8",
    )

    _write_queue(tmp_path, [{
        "new_slug": "new-x", "new_path": str(new_p),
        "target_name": "old-x", "target_path": str(old_p),
        "kind": "decision_reversal", "reason": "r", "confidence": 0.9,
        "resolved": False,
    }])

    cli.main(["resolve", "1", "--action", "supersede", "--apply"])

    new_content = new_p.read_text(encoding="utf-8")
    # supersedes list should have only one "old-x" entry
    match = re.search(r"supersedes:\s*\[(.*?)\]", new_content)
    items = [s.strip() for s in match.group(1).split(",") if s.strip()]
    assert items.count("old-x") == 1


def test_resolve_update_apply_replaces_old_body_deletes_new(tmp_path, monkeypatch):
    from src import contradiction_review_cli as cli
    monkeypatch.setenv("MV3_RUNTIME_DIR", str(tmp_path))

    mem = tmp_path / "memory"
    mem.mkdir()
    new_p = mem / "new_x.md"
    old_p = mem / "old_x.md"
    new_p.write_text(
        "---\nname: new-x\ntype: feedback\n---\n\nfresh body content\n",
        encoding="utf-8",
    )
    old_p.write_text(
        "---\nname: old-x\ntype: feedback\n---\n\nold body content\n",
        encoding="utf-8",
    )

    _write_queue(tmp_path, [{
        "new_slug": "new-x", "new_path": str(new_p),
        "target_name": "old-x", "target_path": str(old_p),
        "kind": "metric_update", "reason": "r", "confidence": 0.9,
        "resolved": False,
    }])

    rc = cli.main(["resolve", "1", "--action", "update", "--apply"])
    assert rc == 0

    old_content = old_p.read_text(encoding="utf-8")
    assert "name: old-x" in old_content  # frontmatter preserved
    assert "fresh body content" in old_content  # body updated
    assert "old body content" not in old_content
    assert not new_p.exists()  # new file deleted


def test_resolve_update_marks_jsonl_resolved(tmp_path, monkeypatch):
    from src import contradiction_review_cli as cli
    monkeypatch.setenv("MV3_RUNTIME_DIR", str(tmp_path))

    mem = tmp_path / "memory"
    mem.mkdir()
    new_p = mem / "new_x.md"
    old_p = mem / "old_x.md"
    new_p.write_text("---\nname: new-x\ntype: feedback\n---\n\nbody\n", encoding="utf-8")
    old_p.write_text("---\nname: old-x\ntype: feedback\n---\n\nbody\n", encoding="utf-8")

    qp = _write_queue(tmp_path, [{
        "new_slug": "new-x", "new_path": str(new_p),
        "target_name": "old-x", "target_path": str(old_p),
        "kind": "metric_update", "reason": "r", "confidence": 0.9,
        "resolved": False,
    }])

    cli.main(["resolve", "1", "--action", "update", "--apply"])

    line = json.loads(qp.read_text(encoding="utf-8").strip())
    assert line["resolved"] == "updated"


def test_resolve_supersede_missing_file_returns_error(tmp_path, monkeypatch):
    from src import contradiction_review_cli as cli
    monkeypatch.setenv("MV3_RUNTIME_DIR", str(tmp_path))

    _write_queue(tmp_path, [{
        "new_slug": "x", "new_path": str(tmp_path / "nonexistent_new.md"),
        "target_name": "y", "target_path": str(tmp_path / "nonexistent_old.md"),
        "kind": "decision_reversal", "reason": "r", "confidence": 0.9,
        "resolved": False,
    }])

    rc = cli.main(["resolve", "1", "--action", "supersede", "--apply"])
    assert rc == 2


def test_resolve_supersede_mark_resolved_failure_returns_2(tmp_path, monkeypatch, capsys):
    """If _mark_resolved fails after frontmatter mutation, return exit 2 with warning."""
    from src import contradiction_review_cli as cli
    monkeypatch.setenv("MV3_RUNTIME_DIR", str(tmp_path))

    mem = tmp_path / "memory"
    mem.mkdir()
    new_p = mem / "new_x.md"
    old_p = mem / "old_x.md"
    new_p.write_text("---\nname: new-x\ntype: feedback\n---\n\nbody\n", encoding="utf-8")
    old_p.write_text("---\nname: old-x\ntype: feedback\n---\n\nbody\n", encoding="utf-8")

    _write_queue(tmp_path, [{
        "new_slug": "new-x", "new_path": str(new_p),
        "target_name": "old-x", "target_path": str(old_p),
        "kind": "decision_reversal", "reason": "r", "confidence": 0.9,
        "resolved": False,
    }])

    # Force _mark_resolved to return False
    monkeypatch.setattr(
        "src.contradiction_review_cli._mark_resolved",
        lambda item, status: False,
    )

    rc = cli.main(["resolve", "1", "--action", "supersede", "--apply"])
    assert rc == 2, f"expected exit 2 on mark_resolved fail, got {rc}"
    err = capsys.readouterr().err
    assert "WARN" in err or "jsonl mark" in err


def test_resolve_update_mark_resolved_failure_returns_2(tmp_path, monkeypatch, capsys):
    """Update: mark_resolved failure → exit 2 + clear warning about already-mutated files."""
    from src import contradiction_review_cli as cli
    monkeypatch.setenv("MV3_RUNTIME_DIR", str(tmp_path))

    mem = tmp_path / "memory"
    mem.mkdir()
    new_p = mem / "new_x.md"
    old_p = mem / "old_x.md"
    new_p.write_text("---\nname: new-x\ntype: feedback\n---\n\nnew body\n", encoding="utf-8")
    old_p.write_text("---\nname: old-x\ntype: feedback\n---\n\nold body\n", encoding="utf-8")

    _write_queue(tmp_path, [{
        "new_slug": "new-x", "new_path": str(new_p),
        "target_name": "old-x", "target_path": str(old_p),
        "kind": "metric_update", "reason": "r", "confidence": 0.9,
        "resolved": False,
    }])

    monkeypatch.setattr(
        "src.contradiction_review_cli._mark_resolved",
        lambda item, status: False,
    )

    rc = cli.main(["resolve", "1", "--action", "update", "--apply"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "WARN" in err


def test_patch_frontmatter_refuses_block_style_yaml_list(tmp_path):
    """Block-style YAML list should be REFUSED (return False), not silently appended."""
    from src.contradiction_review_cli import _patch_frontmatter_list

    p = tmp_path / "x.md"
    p.write_text(
        "---\nname: x\nsupersedes:\n  - a\n  - b\ntype: feedback\n---\n\nbody\n",
        encoding="utf-8",
    )

    original = p.read_text(encoding="utf-8")
    ok = _patch_frontmatter_list(p, "supersedes", "c")

    assert ok is False, "must refuse block-style YAML list, not append duplicate key"
    # File unchanged
    assert p.read_text(encoding="utf-8") == original


def test_patch_frontmatter_flow_style_still_works_after_block_guard(tmp_path):
    """Flow-style supersedes still mutates correctly after block-style guard added."""
    from src.contradiction_review_cli import _patch_frontmatter_list

    p = tmp_path / "x.md"
    p.write_text(
        "---\nname: x\nsupersedes: [a]\ntype: feedback\n---\n\nbody\n",
        encoding="utf-8",
    )

    ok = _patch_frontmatter_list(p, "supersedes", "b")
    assert ok is True
    content = p.read_text(encoding="utf-8")
    # Should now contain "supersedes: [a, b]"
    assert "supersedes: [a, b]" in content
