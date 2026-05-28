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
    # File names (stems) ARE the canonical supersede identifiers (Fix I-name:
    # _supersede_id uses the file stem, not the human `name:` title). Use
    # hyphenated stems so they match the queue's new_slug / target_name.
    new_p = mem / "new-x.md"
    old_p = mem / "old-x.md"
    new_p.write_text(
        "---\nname: New X With Spaces\ntype: feedback\n---\n\nnew body\n",
        encoding="utf-8",
    )
    old_p.write_text(
        "---\nname: Old X With Spaces\ntype: feedback\n---\n\nold body\n",
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
    # stem identifiers in the inline lists; spaced human title NOT leaked in
    assert "supersedes: [old-x]" in new_content
    assert "deprecated_by: [new-x]" in old_content


def test_resolve_supersede_idempotent(tmp_path, monkeypatch):
    """Calling supersede twice does not duplicate the list entry."""
    from src import contradiction_review_cli as cli
    monkeypatch.setenv("MV3_RUNTIME_DIR", str(tmp_path))

    mem = tmp_path / "memory"
    mem.mkdir()
    # Hyphenated stems == supersede identifiers (see _supersede_id).
    new_p = mem / "new-x.md"
    old_p = mem / "old-x.md"
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
    # supersedes list should have only one "old-x" entry (idempotent)
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


# ---------------------------------------------------------------------------
# v3.4 static-audit sweep (5 latent defects + tmp pid minor)
# ---------------------------------------------------------------------------


def test_extract_yaml_name_multiword(tmp_path):
    from src.contradiction_review_cli import _extract_yaml_name
    p = tmp_path / "x.md"
    p.write_text("---\nname: MindVault v3.4 contradiction\ntype: feedback\n---\n\nbody\n", encoding="utf-8")
    name = _extract_yaml_name(p)
    assert name is not None
    assert name != "MindVault"  # not just first token
    # Decision: _extract_yaml_name returns the FULL human title verbatim.
    assert name == "MindVault v3.4 contradiction"


def test_extract_yaml_name_quoted(tmp_path):
    from src.contradiction_review_cli import _extract_yaml_name
    p = tmp_path / "x.md"
    p.write_text('---\nname: "quoted name"\ntype: feedback\n---\n\nbody\n', encoding="utf-8")
    name = _extract_yaml_name(p)
    assert name is not None
    assert '"' not in name  # quotes stripped
    assert name == "quoted name"


def test_extract_yaml_name_single_quoted(tmp_path):
    from src.contradiction_review_cli import _extract_yaml_name
    p = tmp_path / "x.md"
    p.write_text("---\nname: 'single quoted'\ntype: feedback\n---\n\nbody\n", encoding="utf-8")
    name = _extract_yaml_name(p)
    assert name == "single quoted"


def test_extract_yaml_name_single_token_still_works(tmp_path):
    from src.contradiction_review_cli import _extract_yaml_name
    p = tmp_path / "x.md"
    p.write_text("---\nname: simple-slug\ntype: feedback\n---\n\nbody\n", encoding="utf-8")
    assert _extract_yaml_name(p) == "simple-slug"


def test_mark_resolved_preserves_malformed_lines(tmp_path, monkeypatch):
    """A corrupt jsonl line must survive when another row is resolved."""
    from src import contradiction_review_cli as cli
    monkeypatch.setenv("MV3_RUNTIME_DIR", str(tmp_path))
    import json
    p = tmp_path / "contradictions.jsonl"
    good = json.dumps({"new_slug": "a", "target_name": "b", "target_path": "/x/b.md",
                       "kind": "metric_update", "reason": "r", "confidence": 0.9,
                       "ts": "2026-01-01T00:00:00Z", "resolved": False})
    p.write_text(good + "\n" + "CORRUPT NOT JSON\n", encoding="utf-8")

    cli.main(["resolve", "1", "--action", "dismiss", "--apply"])

    lines = p.read_text(encoding="utf-8").splitlines()
    # corrupt line must still be present
    assert any("CORRUPT NOT JSON" in ln for ln in lines), "malformed line was dropped"
    # the good row must now be resolved
    parsed = [json.loads(ln) for ln in lines if ln.strip() and ln.strip() != "CORRUPT NOT JSON"]
    assert parsed[0].get("resolved") == "dismissed"


def test_mark_resolved_disambiguates_duplicate_tuple(tmp_path, monkeypatch):
    """Two rows with same (new_slug, target_name) — resolving #2 must not mark #1."""
    from src import contradiction_review_cli as cli
    monkeypatch.setenv("MV3_RUNTIME_DIR", str(tmp_path))
    import json
    p = tmp_path / "contradictions.jsonl"
    row1 = {"new_slug": "a", "target_name": "b", "target_path": "/x/b.md",
            "kind": "metric_update", "reason": "first", "confidence": 0.9,
            "ts": "2026-01-01T00:00:00Z", "resolved": False}
    row2 = {"new_slug": "a", "target_name": "b", "target_path": "/x/b.md",
            "kind": "fact_correction", "reason": "second", "confidence": 0.8,
            "ts": "2026-01-02T00:00:00Z", "resolved": False}
    p.write_text(json.dumps(row1) + "\n" + json.dumps(row2) + "\n", encoding="utf-8")

    # Resolve index 2 (the fact_correction row)
    cli.main(["resolve", "2", "--action", "dismiss", "--apply"])

    rows = [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    # row1 (first/metric_update) must still be unresolved; row2 resolved
    by_kind = {r["kind"]: r for r in rows}
    assert not by_kind["metric_update"].get("resolved"), "wrong row marked — disambiguation failed"
    assert by_kind["fact_correction"].get("resolved") == "dismissed"


def test_apply_update_unlink_failure_returns_false(tmp_path, monkeypatch):
    from src import contradiction_review_cli as cli
    mem = tmp_path / "memory"
    mem.mkdir()
    new_p = mem / "new_x.md"
    old_p = mem / "old_x.md"
    new_p.write_text("---\nname: new-x\n---\n\nnew\n", encoding="utf-8")
    old_p.write_text("---\nname: old-x\n---\n\nold\n", encoding="utf-8")

    # Make unlink raise a non-missing OSError
    import pathlib
    orig_unlink = pathlib.Path.unlink
    def boom(self, missing_ok=False):
        if self == new_p:
            raise PermissionError("cannot unlink")
        return orig_unlink(self, missing_ok=missing_ok)
    monkeypatch.setattr(pathlib.Path, "unlink", boom)

    from src.contradiction_review_cli import _apply_update
    ok = _apply_update(new_p, old_p)
    assert ok is False, "unlink failure must propagate as False"


def test_apply_supersede_uses_file_stem_not_human_title(tmp_path):
    """supersedes:/deprecated_by: lists must use the file STEM (slug, no spaces),
    not the multi-word frontmatter name — else the inline list is corrupted."""
    from src.contradiction_review_cli import _apply_supersede
    new_p = tmp_path / "new-memory.md"
    old_p = tmp_path / "old-memory.md"
    new_p.write_text("---\nname: New Memory With Spaces\nsupersedes: []\n---\n\nnew\n", encoding="utf-8")
    old_p.write_text("---\nname: Old Memory With Spaces\ndeprecated_by: []\n---\n\nold\n", encoding="utf-8")

    ok = _apply_supersede(new_p, old_p)
    assert ok is True
    new_content = new_p.read_text(encoding="utf-8")
    old_content = old_p.read_text(encoding="utf-8")
    # stems used, NOT the spaced human title (which would break [a, b] inline list)
    assert "supersedes: [old-memory]" in new_content
    assert "deprecated_by: [new-memory]" in old_content
    # the spaced human title must NOT leak into the inline list itself
    assert "supersedes: [Old Memory" not in new_content
    assert "deprecated_by: [New Memory" not in old_content


def test_apply_supersede_no_partial_mutation_when_old_block_style(tmp_path):
    """If OLD cannot be patched (block-style list), NEW must NOT be mutated either."""
    from src.contradiction_review_cli import _apply_supersede
    new_p = tmp_path / "new-memory.md"
    old_p = tmp_path / "old-memory.md"
    new_p.write_text("---\nname: New\nsupersedes: []\n---\n\nnew\n", encoding="utf-8")
    # block-style deprecated_by — _patch_frontmatter_list refuses this.
    # Trailing 'type:' key ensures the list isn't the final fm key (the block
    # detector requires a newline after the last list item).
    old_p.write_text("---\nname: Old\ndeprecated_by:\n  - z\ntype: feedback\n---\n\nold\n", encoding="utf-8")

    new_before = new_p.read_text(encoding="utf-8")
    ok = _apply_supersede(new_p, old_p)
    assert ok is False
    # NEW must be untouched — no half-state
    assert new_p.read_text(encoding="utf-8") == new_before, "NEW mutated despite OLD failure (partial mutation)"


# ---------------------------------------------------------------------------
# v3.4 static-audit round 2 — CRLF frontmatter + block-guard last-key edge
# ---------------------------------------------------------------------------


def test_split_frontmatter_handles_crlf(tmp_path):
    """CRLF-saved memory (Windows/Obsidian) must still have frontmatter detected."""
    from src.contradiction_review_cli import _split_frontmatter
    text = "---\r\nname: a\r\nsupersedes: [b]\r\n---\r\n\r\nbody\r\n"
    fm, body = _split_frontmatter(text)
    assert "name: a" in fm, f"CRLF frontmatter not detected: fm={fm!r}"
    assert "supersedes: [b]" in fm
    assert "body" in body


def test_patch_frontmatter_appends_to_crlf_flow_list(tmp_path):
    """Flow-style patch must work even on CRLF-saved file (frontmatter detected)."""
    from src.contradiction_review_cli import _patch_frontmatter_list
    p = tmp_path / "x.md"
    p.write_bytes("---\r\nname: x\r\nsupersedes: [a]\r\n---\r\n\r\nbody\r\n".encode("utf-8"))
    ok = _patch_frontmatter_list(p, "supersedes", "b")
    assert ok is True, "CRLF flow-style list must be patchable"
    content = p.read_text(encoding="utf-8")
    assert "supersedes: [a, b]" in content


def test_patch_frontmatter_refuses_block_style_as_last_key(tmp_path):
    from src.contradiction_review_cli import _patch_frontmatter_list
    p = tmp_path / "x.md"
    # block list is the LAST key, no trailing key after
    p.write_text("---\nname: x\nsupersedes:\n  - a\n  - b\n---\n\nbody\n", encoding="utf-8")
    original = p.read_text(encoding="utf-8")
    ok = _patch_frontmatter_list(p, "supersedes", "c")
    assert ok is False, "must refuse block-style even as last key"
    assert p.read_text(encoding="utf-8") == original


def test_patch_frontmatter_refuses_single_item_block_as_last_key(tmp_path):
    """Single-item block list as the last key (no trailing newline) must be refused."""
    from src.contradiction_review_cli import _patch_frontmatter_list
    p = tmp_path / "x.md"
    p.write_text("---\nname: x\nsupersedes:\n  - a\n---\n\nbody\n", encoding="utf-8")
    original = p.read_text(encoding="utf-8")
    ok = _patch_frontmatter_list(p, "supersedes", "c")
    assert ok is False, "single-item block-style as last key must be refused"
    assert p.read_text(encoding="utf-8") == original


def test_can_patch_frontmatter_refuses_block_as_last_key(tmp_path):
    """_can_patch_frontmatter_list (dry validation) must mirror the refusal."""
    from src.contradiction_review_cli import _can_patch_frontmatter_list
    p = tmp_path / "x.md"
    p.write_text("---\nname: x\nsupersedes:\n  - a\n---\n\nbody\n", encoding="utf-8")
    assert _can_patch_frontmatter_list(p, "supersedes") is False


def test_mark_resolved_tmp_includes_pid(tmp_path, monkeypatch):
    """tmp file name must include pid to avoid concurrent-resolve races."""
    import os
    from src import contradiction_review_cli as cli
    monkeypatch.setenv("MV3_RUNTIME_DIR", str(tmp_path))
    import json
    p = tmp_path / "contradictions.jsonl"
    row = {"new_slug": "a", "target_name": "b", "kind": "metric_update",
           "reason": "r", "confidence": 0.9, "ts": "2026-01-01T00:00:00Z", "resolved": False}
    p.write_text(json.dumps(row) + "\n", encoding="utf-8")

    captured = {}
    orig_replace = os.replace
    def spy(src, dst):
        captured["src"] = str(src)
        return orig_replace(src, dst)
    monkeypatch.setattr(os, "replace", spy)

    cli.main(["resolve", "1", "--action", "dismiss", "--apply"])
    assert str(os.getpid()) in captured["src"], f"pid not in tmp name: {captured['src']}"
