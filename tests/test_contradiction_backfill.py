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
