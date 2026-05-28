"""T11 — backfill script unit test. Runs against fake memory dir, mocks Gemma."""
from __future__ import annotations
import json
import pytest
from pathlib import Path
import subprocess
import sys


def _run_backfill(args: list[str], cwd: Path):
    """Invoke the script as a subprocess so we exercise its argparse layer."""
    script = cwd / "scripts" / "contradiction_backfill.py"
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
    )


def test_backfill_dry_run_no_queue_mutation(tmp_path, monkeypatch):
    """Dry run reports counts but does not write contradictions.jsonl."""
    import importlib.util
    repo = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "contradiction_backfill",
        repo / "scripts" / "contradiction_backfill.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Set up fake memory dir with 2 files
    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "feedback_a.md").write_text(
        "---\nname: a\ntype: feedback\n---\n\nbody a\n", encoding="utf-8"
    )
    (mem / "feedback_b.md").write_text(
        "---\nname: b\ntype: feedback\n---\n\nbody b\n", encoding="utf-8"
    )

    # Isolate runtime dir so any accidental writes don't hit production
    monkeypatch.setenv("MV3_RUNTIME_DIR", str(tmp_path))

    # Mock detect_contradictions to return fake non-empty result
    repo_src = repo / "src"
    if str(repo_src) not in sys.path:
        sys.path.insert(0, str(repo_src))
    import contradiction_detector
    from contradiction_detector import Contradiction, ContradictionKind

    def fake_detect(candidate, mem_dir):
        return [Contradiction(
            target_path=mem_dir / "other.md", target_name="other",
            kind=ContradictionKind.METRIC_UPDATE, reason="r", confidence=0.9,
        )]
    monkeypatch.setattr(contradiction_detector, "detect_contradictions", fake_detect)

    # Spy on append
    appends = []
    def spy_append(slug, contradictions, new_path):
        appends.append((slug, len(contradictions)))
    monkeypatch.setattr(contradiction_detector, "append_to_review_queue", spy_append)

    rc = mod.main(["--memory-dir", str(mem), "--dry-run"])
    assert rc == 0
    # Dry run must NOT call append
    assert len(appends) == 0
    # Queue file must NOT be created
    assert not (tmp_path / "contradictions.jsonl").exists()


def test_backfill_appends_when_not_dry_run(tmp_path, monkeypatch):
    """Without --dry-run, contradictions are queued."""
    import importlib.util
    repo = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "contradiction_backfill",
        repo / "scripts" / "contradiction_backfill.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "feedback_a.md").write_text(
        "---\nname: a\ntype: feedback\n---\n\nbody\n", encoding="utf-8"
    )

    monkeypatch.setenv("MV3_RUNTIME_DIR", str(tmp_path))

    repo_src = repo / "src"
    if str(repo_src) not in sys.path:
        sys.path.insert(0, str(repo_src))
    import contradiction_detector
    from contradiction_detector import Contradiction, ContradictionKind

    monkeypatch.setattr(
        contradiction_detector, "detect_contradictions",
        lambda c, m: [Contradiction(
            target_path=m / "other.md", target_name="other",
            kind=ContradictionKind.METRIC_UPDATE, reason="r", confidence=0.9,
        )],
    )

    rc = mod.main(["--memory-dir", str(mem)])
    assert rc == 0
    # Append must have written the queue file
    queue = tmp_path / "contradictions.jsonl"
    assert queue.exists()
    rows = [json.loads(line) for line in queue.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) >= 1


def test_backfill_skips_memory_md_index(tmp_path, monkeypatch):
    """MEMORY.md (the index file, no frontmatter) must be skipped."""
    import importlib.util
    repo = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "contradiction_backfill",
        repo / "scripts" / "contradiction_backfill.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "MEMORY.md").write_text("# Index\n- link", encoding="utf-8")
    (mem / "feedback_a.md").write_text(
        "---\nname: a\ntype: feedback\n---\n\nbody\n", encoding="utf-8"
    )

    monkeypatch.setenv("MV3_RUNTIME_DIR", str(tmp_path))

    repo_src = repo / "src"
    if str(repo_src) not in sys.path:
        sys.path.insert(0, str(repo_src))
    import contradiction_detector

    seen_slugs = []
    def spy(candidate, mem_dir):
        seen_slugs.append(candidate["slug"])
        return []
    monkeypatch.setattr(contradiction_detector, "detect_contradictions", spy)

    rc = mod.main(["--memory-dir", str(mem), "--dry-run"])
    assert rc == 0
    # MEMORY.md must not be in the candidate slugs
    assert "MEMORY" not in seen_slugs
    assert "feedback_a" in seen_slugs


def test_backfill_limit_arg(tmp_path, monkeypatch):
    """--limit N processes only first N files."""
    import importlib.util
    repo = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "contradiction_backfill",
        repo / "scripts" / "contradiction_backfill.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    mem = tmp_path / "memory"
    mem.mkdir()
    for i in range(5):
        (mem / f"feedback_{i}.md").write_text(
            f"---\nname: f{i}\n---\n\nbody\n", encoding="utf-8"
        )

    monkeypatch.setenv("MV3_RUNTIME_DIR", str(tmp_path))

    repo_src = repo / "src"
    if str(repo_src) not in sys.path:
        sys.path.insert(0, str(repo_src))
    import contradiction_detector

    seen = []
    def spy(candidate, mem_dir):
        seen.append(candidate["slug"])
        return []
    monkeypatch.setattr(contradiction_detector, "detect_contradictions", spy)

    rc = mod.main(["--memory-dir", str(mem), "--limit", "2", "--dry-run"])
    assert rc == 0
    assert len(seen) == 2  # only first 2 processed


def test_backfill_missing_memory_dir_returns_1(tmp_path):
    """Non-existent --memory-dir returns exit code 1."""
    import importlib.util
    repo = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "contradiction_backfill",
        repo / "scripts" / "contradiction_backfill.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    rc = mod.main(["--memory-dir", str(tmp_path / "does-not-exist")])
    assert rc == 1


def test_backfill_default_memory_dir_uses_home(monkeypatch):
    """Default memory dir must derive from $HOME, not hardcoded username slug.

    Public-ship sanitize: forbids "-Users-yonghaekim" or any other hardcoded
    user-slug literal. Mirrors session_memory_end._default_memory_dir's pattern.
    """
    import importlib.util
    repo = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "contradiction_backfill",
        repo / "scripts" / "contradiction_backfill.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Clear any override env so we hit the home_slug default branch.
    monkeypatch.delenv("MV3_MEMORY_DIR", raising=False)
    monkeypatch.delenv("MV3_PROJECTS_DIR", raising=False)
    monkeypatch.delenv("MV3_PROJECTS_ROOT", raising=False)

    default = mod._default_memory_dir()
    home = str(Path.home())

    # Default must derive from the current $HOME (its slug form appears in the path).
    expected_slug = "-" + home.strip("/").replace("/", "-")
    assert expected_slug in str(default), (
        f"Default memory dir {default} must derive from $HOME={home} "
        f"(expected slug {expected_slug}), not a hardcoded user literal."
    )
    # And the literal author slug must not be present for any other user.
    if expected_slug != "-Users-yonghaekim":
        assert "-Users-yonghaekim" not in str(default), (
            "hardcoded author slug '-Users-yonghaekim' detected — "
            "must derive from Path.home()"
        )


def test_backfill_warns_on_custom_memory_dir(tmp_path, monkeypatch, capsys):
    """A --memory-dir that differs from the indexed default must warn loudly.

    Defect I-memdir: recall_memory reads the production index DB, not the passed
    directory. A custom (un-indexed) --memory-dir therefore silently detects zero
    contradictions. The script must surface this limitation on stderr.
    """
    import importlib.util
    repo = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "contradiction_backfill", repo / "scripts" / "contradiction_backfill.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # Ensure the default derives from $HOME (not our custom dir).
    monkeypatch.delenv("MV3_MEMORY_DIR", raising=False)
    monkeypatch.delenv("MV3_PROJECTS_DIR", raising=False)
    monkeypatch.delenv("MV3_PROJECTS_ROOT", raising=False)

    custom = tmp_path / "custom_mem"
    custom.mkdir()
    (custom / "feedback_a.md").write_text("---\nname: a\n---\nbody\n", encoding="utf-8")

    # mock detect to avoid Gemma
    repo_src = repo / "src"
    if str(repo_src) not in sys.path:
        sys.path.insert(0, str(repo_src))
    import contradiction_detector
    monkeypatch.setattr(contradiction_detector, "detect_contradictions", lambda c, m: [])

    mod.main(["--memory-dir", str(custom), "--dry-run"])
    err = capsys.readouterr().err
    assert "index" in err.lower() or "warning" in err.lower(), (
        "custom dir should warn about index DB"
    )


def test_backfill_no_warn_on_default_memory_dir(tmp_path, monkeypatch, capsys):
    """When --memory-dir matches the (env-overridden) default, NO warning fires."""
    import importlib.util
    repo = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "contradiction_backfill", repo / "scripts" / "contradiction_backfill.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    mem = tmp_path / "memory"
    mem.mkdir()
    (mem / "feedback_a.md").write_text("---\nname: a\n---\nbody\n", encoding="utf-8")
    # Point the DEFAULT at this dir via env so passing it explicitly matches.
    monkeypatch.setenv("MV3_MEMORY_DIR", str(mem))

    repo_src = repo / "src"
    if str(repo_src) not in sys.path:
        sys.path.insert(0, str(repo_src))
    import contradiction_detector
    monkeypatch.setattr(contradiction_detector, "detect_contradictions", lambda c, m: [])

    # No --memory-dir at all → uses default → no warning.
    mod.main(["--dry-run"])
    err = capsys.readouterr().err
    assert "differs from the indexed" not in err, (
        f"default memory dir must not warn, got stderr: {err!r}"
    )


def test_backfill_default_memory_dir_respects_env_overrides(monkeypatch, tmp_path):
    """MV3_MEMORY_DIR / MV3_PROJECTS_DIR overrides honored (matches hook)."""
    import importlib.util
    repo = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "contradiction_backfill",
        repo / "scripts" / "contradiction_backfill.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    # MV3_MEMORY_DIR wins outright.
    monkeypatch.setenv("MV3_MEMORY_DIR", str(tmp_path / "explicit"))
    assert mod._default_memory_dir() == tmp_path / "explicit"

    # MV3_PROJECTS_DIR appends /memory.
    monkeypatch.delenv("MV3_MEMORY_DIR")
    monkeypatch.setenv("MV3_PROJECTS_DIR", str(tmp_path / "slot"))
    assert mod._default_memory_dir() == tmp_path / "slot" / "memory"
