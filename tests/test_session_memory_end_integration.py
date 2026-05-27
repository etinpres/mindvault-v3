"""T5 integration tests for contradiction detection hook in session_memory_end."""
from __future__ import annotations
import pytest
from pathlib import Path
from unittest.mock import MagicMock


@pytest.fixture
def fake_write_staged(tmp_path):
    """Stub write_staged that writes a real file and returns its path."""
    def _stub(item, session_id, slug_override=None):
        slug = slug_override or item.get("slug", "x")
        p = tmp_path / "memory" / f"{item.get('type', 'feedback')}_{slug}.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f"---\nname: {slug}\n---\n\n{item.get('body', '')}\n",
                     encoding="utf-8")
        return p
    return _stub


def test_make_contradiction_aware_writer_calls_detect(tmp_path, fake_write_staged,
                                                       monkeypatch):
    """Wrapped writer calls detect_contradictions with candidate including 'path'."""
    from session_memory_end import make_contradiction_aware_writer

    calls = []
    monkeypatch.setattr(
        "contradiction_detector.detect_contradictions",
        lambda c, m: calls.append(c) or [],
    )
    monkeypatch.setattr(
        "contradiction_detector.append_to_review_queue",
        lambda *args, **kwargs: None,
    )

    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    wrapped = make_contradiction_aware_writer(fake_write_staged, mem_dir)

    item = {"slug": "my-thing", "title": "T", "body": "B", "type": "feedback"}
    result = wrapped(item, "sid123", slug_override="my-thing")

    assert result is not None
    assert len(calls) == 1
    assert calls[0]["slug"] == "my-thing"
    assert "path" in calls[0], "candidate must carry 'path' for path-identity self-exclusion (T2 contract)"
    assert calls[0]["path"] == result


def test_writer_calls_append_when_contradictions_found(tmp_path, fake_write_staged,
                                                       monkeypatch):
    """When detect_contradictions returns non-empty, append_to_review_queue is called."""
    from session_memory_end import make_contradiction_aware_writer
    from contradiction_detector import Contradiction, ContradictionKind

    fake_c = [Contradiction(target_path=tmp_path/"a.md", target_name="a",
                            kind=ContradictionKind.METRIC_UPDATE, reason="r",
                            confidence=0.9)]
    monkeypatch.setattr(
        "contradiction_detector.detect_contradictions",
        lambda c, m: fake_c,
    )

    append_calls = []
    monkeypatch.setattr(
        "contradiction_detector.append_to_review_queue",
        lambda slug, contradictions, new_path: append_calls.append(
            (slug, len(contradictions), new_path)),
    )

    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    wrapped = make_contradiction_aware_writer(fake_write_staged, mem_dir)

    item = {"slug": "my-thing", "title": "T", "body": "B", "type": "feedback"}
    wrapped(item, "sid123", slug_override="my-thing")

    assert len(append_calls) == 1
    slug, count, new_path = append_calls[0]
    assert slug == "my-thing"
    assert count == 1
    assert "my-thing" in str(new_path)


def test_writer_swallows_detector_exceptions(tmp_path, fake_write_staged, monkeypatch):
    """If detect_contradictions raises, writer still returns the staged path."""
    from session_memory_end import make_contradiction_aware_writer

    def boom(candidate, mem_dir):
        raise RuntimeError("detector crashed")
    monkeypatch.setattr("contradiction_detector.detect_contradictions", boom)

    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    wrapped = make_contradiction_aware_writer(fake_write_staged, mem_dir)

    item = {"slug": "my-thing", "title": "T", "body": "B", "type": "feedback"}
    result = wrapped(item, "sid123", slug_override="my-thing")

    # Must not raise — staged write succeeded, contradiction detection is best-effort
    assert result is not None


def test_writer_skips_detection_when_write_staged_returns_none(tmp_path, monkeypatch):
    """If underlying write_staged returns None (dedup skip), no detection fires."""
    from session_memory_end import make_contradiction_aware_writer

    detect_calls = []
    monkeypatch.setattr(
        "contradiction_detector.detect_contradictions",
        lambda c, m: detect_calls.append(c) or [],
    )

    def stub_returns_none(item, session_id, slug_override=None):
        return None  # simulate dedup skip

    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    wrapped = make_contradiction_aware_writer(stub_returns_none, mem_dir)

    item = {"slug": "my-thing", "title": "T", "body": "B", "type": "feedback"}
    result = wrapped(item, "sid123", slug_override="my-thing")

    assert result is None
    assert len(detect_calls) == 0
