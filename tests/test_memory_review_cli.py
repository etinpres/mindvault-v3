"""memory_review_cli.cmd_approve tests.

Focus (v3.4 static-audit round 2, Defect Suspect2): when a STAGED file carries
``supersedes:`` / ``deprecated_by:`` frontmatter (injected by
contradiction_review_cli resolve --action supersede), cmd_approve must
PASS THROUGH those keys into the promoted frontmatter instead of silently
discarding them on the frontmatter rebuild.

The module computes MEMORY_DIR-derived constants at import time, so we load a
fresh module instance per test with MV3_MEMORY_DIR pointed at a tmp dir.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _load_cli(monkeypatch, mem_dir: Path, data_dir: Path):
    """Load memory_review_cli with MEMORY_DIR/data dir pointed at tmp paths.

    Constants (MEMORY_DIR, STAGED_DIR, ...) are evaluated at import time, so we
    set env first and load a unique module instance to avoid cross-test bleed.
    """
    monkeypatch.setenv("MV3_MEMORY_DIR", str(mem_dir))
    monkeypatch.setenv("MV3_DATA_DIR", str(data_dir))
    monkeypatch.delenv("MV3_PROJECTS_DIR", raising=False)
    monkeypatch.delenv("MV3_PROJECTS_ROOT", raising=False)
    spec = importlib.util.spec_from_file_location(
        f"memory_review_cli_{mem_dir.name}", REPO / "src" / "memory_review_cli.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _capture_json(capsys) -> dict:
    out = capsys.readouterr().out
    return json.loads(out)


def test_approve_new_passes_through_supersedes(tmp_path, monkeypatch, capsys):
    """New-promote flow: staged supersedes: [...] must survive into promoted file."""
    mem = tmp_path / "memory"
    (mem / "_staged").mkdir(parents=True)
    cli = _load_cli(monkeypatch, mem, tmp_path / "data")

    staged = mem / "_staged" / "20260101-000000_feedback_new_note.md"
    staged.write_text(
        "---\n"
        "name: New Note\n"
        "description: a fresh note\n"
        "type: feedback\n"
        "supersedes: [old_thing]\n"
        "---\n\n"
        "body text\n",
        encoding="utf-8",
    )

    rc = cli.cmd_approve(staged.name)
    assert rc == 0
    res = _capture_json(capsys)
    assert res.get("ok") is True, res

    promoted = mem / "new_note.md"
    assert promoted.is_file(), "promoted file missing"
    content = promoted.read_text(encoding="utf-8")
    assert "supersedes: [old_thing]" in content, (
        f"supersedes dropped on promote: {content!r}"
    )
    # staged file consumed
    assert not staged.exists()


def test_approve_new_passes_through_deprecated_by(tmp_path, monkeypatch, capsys):
    """deprecated_by: [...] in a staged new file must also survive promotion."""
    mem = tmp_path / "memory"
    (mem / "_staged").mkdir(parents=True)
    cli = _load_cli(monkeypatch, mem, tmp_path / "data")

    staged = mem / "_staged" / "20260101-000000_feedback_dep_note.md"
    staged.write_text(
        "---\n"
        "name: Dep Note\n"
        "type: feedback\n"
        "deprecated_by: [newer_thing]\n"
        "---\n\n"
        "body\n",
        encoding="utf-8",
    )

    rc = cli.cmd_approve(staged.name)
    assert rc == 0
    res = _capture_json(capsys)
    assert res.get("ok") is True, res

    content = (mem / "dep_note.md").read_text(encoding="utf-8")
    assert "deprecated_by: [newer_thing]" in content, content


def test_approve_new_without_supersede_keys_unchanged(tmp_path, monkeypatch, capsys):
    """No supersedes/deprecated_by → promoted frontmatter has neither key."""
    mem = tmp_path / "memory"
    (mem / "_staged").mkdir(parents=True)
    cli = _load_cli(monkeypatch, mem, tmp_path / "data")

    staged = mem / "_staged" / "20260101-000000_feedback_plain.md"
    staged.write_text(
        "---\nname: Plain\ntype: feedback\n---\n\nbody\n", encoding="utf-8"
    )

    rc = cli.cmd_approve(staged.name)
    assert rc == 0
    res = _capture_json(capsys)
    assert res.get("ok") is True, res

    content = (mem / "plain.md").read_text(encoding="utf-8")
    assert "supersedes:" not in content
    assert "deprecated_by:" not in content


def test_approve_update_passes_through_supersedes(tmp_path, monkeypatch, capsys):
    """Update flow: staged supersedes: [...] must survive into the overwritten target."""
    mem = tmp_path / "memory"
    (mem / "_staged").mkdir(parents=True)
    # _is_safe_update_target checks the target lives under an allowed memory root.
    # _extra_memory_dirs() reads MV3_EXTRA_MEMORY_DIRS at call time, so register
    # our tmp mem dir there.
    monkeypatch.setenv("MV3_EXTRA_MEMORY_DIRS", str(mem))
    cli = _load_cli(monkeypatch, mem, tmp_path / "data")

    # Existing promoted target (the update_of points here).
    target = mem / "existing.md"
    target.write_text(
        "---\nname: Existing\ndescription: old desc\ntype: feedback\n---\n\nold body\n",
        encoding="utf-8",
    )

    staged = mem / "_staged" / "20260101-000000_feedback_existing.md"
    staged.write_text(
        "---\n"
        "name: Existing\n"
        "type: feedback\n"
        f"update_of: {target}\n"
        "supersedes: [some_old]\n"
        "---\n\n"
        "refined body\n",
        encoding="utf-8",
    )

    rc = cli.cmd_approve(staged.name)
    assert rc == 0
    res = _capture_json(capsys)
    assert res.get("ok") is True, res
    assert res.get("kind") == "update", res

    content = target.read_text(encoding="utf-8")
    assert "refined body" in content, "update body not merged"
    assert "supersedes: [some_old]" in content, (
        f"supersedes dropped on update-promote: {content!r}"
    )


# ---------------------------------------------------------------------------
# Phase 1 Provenance — source_type/source_ref/captured_at preservation tests
# ---------------------------------------------------------------------------

def _load_sme(monkeypatch, mem_dir: Path, data_dir: Path):
    """Load session_memory_end with MEMORY_DIR pointed at tmp paths."""
    import importlib.util as ilu
    monkeypatch.setenv("MV3_MEMORY_DIR", str(mem_dir))
    monkeypatch.setenv("MV3_DATA_DIR", str(data_dir))
    monkeypatch.delenv("MV3_PROJECTS_DIR", raising=False)
    monkeypatch.delenv("MV3_PROJECTS_ROOT", raising=False)
    spec = ilu.spec_from_file_location(
        f"session_memory_end_{mem_dir.name}", REPO / "src" / "session_memory_end.py"
    )
    mod = ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_approve_new_preserves_provenance(tmp_path, monkeypatch, capsys):
    """NEW-PROMOTE: source_type/source_ref/captured_at must survive into promoted file.

    This is the completion-gate regression: write_staged records provenance into
    the staged frontmatter, but cmd_approve was rebuilding frontmatter from scratch
    and stripping source_type/source_ref/staged_at on promotion.
    """
    mem = tmp_path / "memory"
    (mem / "_staged").mkdir(parents=True)

    # Stage via real write_staged so the staged file has the full provenance block.
    sme = _load_sme(monkeypatch, mem, tmp_path / "data")
    session_id = "abcdef1234567890abcdef1234567890"
    staged_path = sme.write_staged(
        {
            "title": "prov test note",
            "type": "feedback",
            "reason": "test reason",
            "evidence": "test evidence",
            "body": "provenance body text",
        },
        session_id=session_id,
        source_type="session",
        source_ref=session_id,
    )
    assert staged_path is not None, "write_staged returned None"
    assert staged_path.is_file(), "staged file not created"

    # Verify staged file actually has source_type/source_ref/staged_at
    staged_text = staged_path.read_text(encoding="utf-8")
    assert "source_type: session" in staged_text, "write_staged did not embed source_type"
    assert f"source_ref: {session_id}" in staged_text, "write_staged did not embed source_ref"
    assert "staged_at:" in staged_text, "write_staged did not embed staged_at"

    # Now approve via cmd_approve — reload cli with same env
    cli = _load_cli(monkeypatch, mem, tmp_path / "data")
    rc = cli.cmd_approve(staged_path.name)
    assert rc == 0
    res = _capture_json(capsys)
    assert res.get("ok") is True, res

    # Promoted permanent file must carry provenance fields
    slug = cli._promoted_slug(staged_path.name)
    promoted = mem / f"{slug}.md"
    assert promoted.is_file(), f"promoted file missing at {promoted}"

    content = promoted.read_text(encoding="utf-8")
    assert "source_type: session" in content, (
        f"source_type stripped on promote:\n{content}"
    )
    assert f"source_ref: {session_id}" in content, (
        f"source_ref stripped on promote:\n{content}"
    )
    # captured_at (from staged_at) must be present so recall can render the date
    assert "captured_at:" in content, (
        f"captured_at (from staged_at) stripped on promote:\n{content}"
    )
    # staged file must be consumed
    assert not staged_path.exists(), "staged file should be deleted after approve"


def test_approve_update_preserves_existing_provenance(tmp_path, monkeypatch, capsys):
    """UPDATE path: existing target's provenance takes precedence over staged meta.

    When updating an existing permanent memory, the original source_type/source_ref
    must be preserved (memory's origin doesn't change just because body is refined).
    """
    mem = tmp_path / "memory"
    (mem / "_staged").mkdir(parents=True)
    monkeypatch.setenv("MV3_EXTRA_MEMORY_DIRS", str(mem))

    cli = _load_cli(monkeypatch, mem, tmp_path / "data")

    # Existing permanent memory WITH its original provenance
    target = mem / "existing_prov.md"
    target.write_text(
        "---\n"
        "name: Existing Prov\n"
        "description: existing desc\n"
        "type: feedback\n"
        "source_type: session\n"
        "source_ref: original-session-id-0000\n"
        "captured_at: 2026-01-01T10:00:00\n"
        "---\n\n"
        "old body\n",
        encoding="utf-8",
    )

    # Staged update with DIFFERENT source provenance
    staged = mem / "_staged" / "20260201-000000_feedback_existing_prov.md"
    staged.write_text(
        "---\n"
        "name: Existing Prov\n"
        "type: feedback\n"
        f"update_of: {target}\n"
        "source_type: session\n"
        "source_ref: new-session-id-9999\n"
        "staged_at: 2026-02-01T12:00:00\n"
        "---\n\n"
        "refined body\n",
        encoding="utf-8",
    )

    rc = cli.cmd_approve(staged.name)
    assert rc == 0
    res = _capture_json(capsys)
    assert res.get("ok") is True, res
    assert res.get("kind") == "update", res

    content = target.read_text(encoding="utf-8")
    assert "refined body" in content, "update body not merged"
    # Existing provenance must be preserved (original origin wins)
    assert "source_type: session" in content, f"source_type missing: {content}"
    assert "source_ref: original-session-id-0000" in content, (
        f"existing source_ref was overwritten by staged: {content}"
    )
    assert "captured_at: 2026-01-01T10:00:00" in content, (
        f"existing captured_at was overwritten: {content}"
    )


def test_approve_update_provenance_fallback_to_staged(tmp_path, monkeypatch, capsys):
    """UPDATE path: when existing target has NO provenance, staged provenance is used.

    Coverage-locking test for the ``existing_meta.get(...) or meta.get(...)``
    fallback in cmd_approve's update branch.  An old pre-fix memory file that
    was promoted before provenance tracking existed has no source_type/source_ref;
    after update-approve the promoted file should carry the STAGED provenance.
    """
    mem = tmp_path / "memory"
    (mem / "_staged").mkdir(parents=True)
    monkeypatch.setenv("MV3_EXTRA_MEMORY_DIRS", str(mem))

    cli = _load_cli(monkeypatch, mem, tmp_path / "data")

    # Existing permanent memory WITHOUT provenance (old pre-fix file)
    target = mem / "no_prov.md"
    target.write_text(
        "---\n"
        "name: No Prov\n"
        "description: old desc without provenance\n"
        "type: feedback\n"
        "---\n\n"
        "old body\n",
        encoding="utf-8",
    )

    # Staged update WITH provenance (the new session that refined the body)
    staged = mem / "_staged" / "20260301-000000_feedback_no_prov.md"
    staged.write_text(
        "---\n"
        "name: No Prov\n"
        "type: feedback\n"
        f"update_of: {target}\n"
        "source_type: session\n"
        "source_ref: fallback-session-id-1234\n"
        "staged_at: 2026-03-01T09:00:00\n"
        "---\n\n"
        "refined body with provenance\n",
        encoding="utf-8",
    )

    rc = cli.cmd_approve(staged.name)
    assert rc == 0
    res = _capture_json(capsys)
    assert res.get("ok") is True, res
    assert res.get("kind") == "update", res

    content = target.read_text(encoding="utf-8")
    assert "refined body with provenance" in content, "update body not merged"
    # Staged provenance must be used because existing had none
    assert "source_type: session" in content, (
        f"source_type missing (fallback not applied): {content}"
    )
    assert "source_ref: fallback-session-id-1234" in content, (
        f"staged source_ref not used as fallback: {content}"
    )
    assert "captured_at:" in content, (
        f"captured_at missing (from staged_at fallback): {content}"
    )
