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
