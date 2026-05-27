import pytest
from pathlib import Path
from src.contradiction_detector import (
    Contradiction, ContradictionKind, detect_contradictions,
)

def test_contradiction_kind_enum_values():
    assert ContradictionKind.METRIC_UPDATE.value == "metric_update"
    assert ContradictionKind.DECISION_REVERSAL.value == "decision_reversal"
    assert ContradictionKind.FACT_CORRECTION.value == "fact_correction"
    assert ContradictionKind.NO_CONFLICT.value == "no_conflict"

def test_contradiction_dataclass_minimal():
    c = Contradiction(
        target_path=Path("/x/a.md"),
        target_name="a",
        kind=ContradictionKind.METRIC_UPDATE,
        reason="hit rate 65 -> 66.3",
        confidence=0.85,
    )
    assert c.kind == ContradictionKind.METRIC_UPDATE
    assert 0 <= c.confidence <= 1
    assert c.new_body_excerpt == ""

def test_detect_returns_empty_when_mem_dir_empty(tmp_path):
    candidate = {"slug": "x", "title": "X", "body": "no related"}
    result = detect_contradictions(candidate, tmp_path)
    assert result == []


def test_recall_candidates_excludes_self_slug(tmp_path, write_memory, monkeypatch):
    from src.contradiction_detector import _recall_candidates

    own = write_memory(tmp_path, "new_metric.md",
                       "name: new-metric\ntype: feedback", "hit rate 66.3%")
    other = write_memory(tmp_path, "old_metric.md",
                         "name: old-metric\ntype: feedback", "hit rate 65%")

    def fake_hybrid(query, mem_dir, top_k):
        return [(own, 0.95), (other, 0.85)]
    monkeypatch.setattr("src.contradiction_detector._hybrid_search", fake_hybrid)

    candidate = {"slug": "new-metric", "title": "회수율", "body": "hit rate 66.3%"}
    found = _recall_candidates(candidate, tmp_path, top_k=5)

    paths = [p for p, _ in found]
    assert own not in paths
    assert other in paths


def test_recall_candidates_excludes_self_by_explicit_path(tmp_path, write_memory, monkeypatch):
    """When candidate carries 'path', exclude by path identity (most reliable)."""
    from src.contradiction_detector import _recall_candidates

    own = write_memory(tmp_path, "feedback_new_metric.md",
                       "name: new-metric\ntype: feedback", "hit 66.3%")
    other = write_memory(tmp_path, "feedback_old_metric.md",
                         "name: old-metric\ntype: feedback", "hit 65%")

    monkeypatch.setattr(
        "src.contradiction_detector._hybrid_search",
        lambda q, m, top_k: [(own, 0.95), (other, 0.85)],
    )

    # Note: slug="new-metric" but stem is "feedback_new_metric" (type prefix).
    # The current stem-only logic would FAIL to exclude own. Path identity catches it.
    candidate = {"slug": "new-metric", "title": "회수율", "body": "hit 66.3%",
                 "path": own}
    found = _recall_candidates(candidate, tmp_path)
    paths = [p for p, _ in found]
    assert own not in paths
    assert other in paths


def test_recall_candidates_fallback_stem_suffix_match(tmp_path, write_memory, monkeypatch):
    """When no 'path' in candidate, fall back to stem suffix match (handles <type>_<slug>)."""
    from src.contradiction_detector import _recall_candidates

    own = write_memory(tmp_path, "procedural_my_thing.md",
                       "name: my-thing\ntype: procedural", "body")
    other = write_memory(tmp_path, "feedback_other.md",
                         "name: other\ntype: feedback", "body")

    monkeypatch.setattr(
        "src.contradiction_detector._hybrid_search",
        lambda q, m, top_k: [(own, 0.95), (other, 0.85)],
    )

    candidate = {"slug": "my-thing", "title": "t", "body": "b"}  # no "path"
    found = _recall_candidates(candidate, tmp_path)
    paths = [p for p, _ in found]
    assert own not in paths, f"stem suffix match failed: own={own.stem} should match slug=my-thing"
    assert other in paths


def test_hybrid_search_logs_debug_on_recall_failure(tmp_path, monkeypatch):
    """recall_memory raising → _debug log + return [] (no propagation)."""
    from src.contradiction_detector import _hybrid_search

    monkeypatch.setenv("MV3_RUNTIME_DIR", str(tmp_path))

    def boom(query, top_k):
        raise RuntimeError("simulated DB down")
    monkeypatch.setattr("src.memory_search.recall_memory", boom)

    result = _hybrid_search("any query", tmp_path)
    assert result == []

    log = tmp_path / "debug.log"
    assert log.exists()
    contents = log.read_text(encoding="utf-8")
    assert "recall_memory failed" in contents
    assert "simulated DB down" in contents


def test_classify_metric_update(monkeypatch):
    from src.contradiction_detector import _classify_pair
    monkeypatch.setattr(
        "src.contradiction_detector._call_gemma_for_classify",
        lambda p, max_tokens=400: '{"kind": "metric_update", "reason": "65% → 66.3%", "confidence": 0.92}',
    )
    result = _classify_pair("hit rate 66.3% (n=3,193)", "hit rate 65% (n=2,397)")
    assert result["kind"] == "metric_update"
    assert result["confidence"] >= 0.8


def test_classify_no_conflict_unrelated(monkeypatch):
    from src.contradiction_detector import _classify_pair
    monkeypatch.setattr(
        "src.contradiction_detector._call_gemma_for_classify",
        lambda p, max_tokens=400: '{"kind": "no_conflict", "reason": "주제 다름", "confidence": 0.95}',
    )
    assert _classify_pair("python tip", "scanner CLI")["kind"] == "no_conflict"


def test_classify_handles_gemma_failure(monkeypatch):
    from src.contradiction_detector import _classify_pair
    monkeypatch.setattr(
        "src.contradiction_detector._call_gemma_for_classify",
        lambda p, max_tokens=400: None,
    )
    assert _classify_pair("a", "b") is None


def test_classify_handles_malformed_json(monkeypatch):
    from src.contradiction_detector import _classify_pair
    monkeypatch.setattr(
        "src.contradiction_detector._call_gemma_for_classify",
        lambda p, max_tokens=400: "not json {{",
    )
    assert _classify_pair("a", "b") is None


def test_classify_strips_code_fences(monkeypatch):
    from src.contradiction_detector import _classify_pair
    monkeypatch.setattr(
        "src.contradiction_detector._call_gemma_for_classify",
        lambda p, max_tokens=400: '```json\n{"kind": "fact_correction", "reason": "r", "confidence": 0.8}\n```',
    )
    assert _classify_pair("a", "b")["kind"] == "fact_correction"


def test_classify_strips_fences_without_trailing_newline(monkeypatch):
    """Gemma sometimes emits ```json\\n{...}``` (no \\n before closing fence)."""
    from src.contradiction_detector import _classify_pair
    monkeypatch.setattr(
        "src.contradiction_detector._call_gemma_for_classify",
        lambda p, max_tokens=400: '```json\n{"kind": "metric_update", "reason": "r", "confidence": 0.8}```',
    )
    result = _classify_pair("a", "b")
    assert result is not None, "should strip fence even without trailing newline"
    assert result["kind"] == "metric_update"


def test_classify_strips_inline_fences(monkeypatch):
    """Inline single-line fence: ```{...}```"""
    from src.contradiction_detector import _classify_pair
    monkeypatch.setattr(
        "src.contradiction_detector._call_gemma_for_classify",
        lambda p, max_tokens=400: '```{"kind": "fact_correction", "reason": "r", "confidence": 0.9}```',
    )
    result = _classify_pair("a", "b")
    assert result is not None, "should strip inline fence"
    assert result["kind"] == "fact_correction"


def test_detect_end_to_end_metric_drift(tmp_path, write_memory, monkeypatch):
    from src.contradiction_detector import detect_contradictions, ContradictionKind

    old = write_memory(tmp_path, "feedback_old_metric.md",
                       "name: old-metric\ndescription: 회수율\ntype: feedback",
                       "hit rate 65% (n=2,397)")

    monkeypatch.setattr(
        "src.contradiction_detector._recall_candidates",
        lambda c, m, top_k=5: [(old, 0.85)],
    )
    monkeypatch.setattr(
        "src.contradiction_detector._classify_pair",
        lambda new, old_body: {"kind": "metric_update", "reason": "65 → 66.3", "confidence": 0.9},
    )

    candidate = {"slug": "new-metric", "title": "회수율 재측",
                 "body": "hit rate 66.3% (n=3,193)"}
    result = detect_contradictions(candidate, tmp_path)

    assert len(result) == 1
    assert result[0].kind == ContradictionKind.METRIC_UPDATE
    assert result[0].confidence == 0.9
    assert "65" in result[0].old_body_excerpt
    assert "66.3" in result[0].new_body_excerpt
    # excerpt is BODY_EXCERPT_CHARS-bounded
    assert len(result[0].new_body_excerpt) <= 200
    assert len(result[0].old_body_excerpt) <= 200


def test_detect_filters_low_confidence(tmp_path, write_memory, monkeypatch):
    from src.contradiction_detector import detect_contradictions
    old = write_memory(tmp_path, "x.md", "name: x\ntype: feedback", "old text")
    monkeypatch.setattr(
        "src.contradiction_detector._recall_candidates",
        lambda c, m, top_k=5: [(old, 0.7)],
    )
    monkeypatch.setattr(
        "src.contradiction_detector._classify_pair",
        lambda new, old_body: {"kind": "fact_correction", "reason": "?", "confidence": 0.5},
    )
    assert detect_contradictions({"slug": "y", "body": "new", "title": "Y"}, tmp_path) == []


def test_detect_filters_no_conflict(tmp_path, write_memory, monkeypatch):
    from src.contradiction_detector import detect_contradictions
    old = write_memory(tmp_path, "x.md", "name: x\ntype: feedback", "old text")
    monkeypatch.setattr(
        "src.contradiction_detector._recall_candidates",
        lambda c, m, top_k=5: [(old, 0.85)],
    )
    monkeypatch.setattr(
        "src.contradiction_detector._classify_pair",
        lambda new, old_body: {"kind": "no_conflict", "reason": "주제 다름", "confidence": 0.95},
    )
    assert detect_contradictions({"slug": "y", "body": "new", "title": "Y"}, tmp_path) == []


def test_detect_handles_classify_failure(tmp_path, write_memory, monkeypatch):
    """_classify_pair returning None → skip that candidate, don't crash."""
    from src.contradiction_detector import detect_contradictions
    old = write_memory(tmp_path, "x.md", "name: x\ntype: feedback", "old text")
    monkeypatch.setattr(
        "src.contradiction_detector._recall_candidates",
        lambda c, m, top_k=5: [(old, 0.85)],
    )
    monkeypatch.setattr(
        "src.contradiction_detector._classify_pair",
        lambda new, old_body: None,
    )
    assert detect_contradictions({"slug": "y", "body": "new", "title": "Y"}, tmp_path) == []


def test_append_to_review_queue_writes_jsonl(tmp_path, monkeypatch):
    from src.contradiction_detector import (
        append_to_review_queue, Contradiction, ContradictionKind,
    )
    monkeypatch.setenv("MV3_RUNTIME_DIR", str(tmp_path))

    c = [Contradiction(
        target_path=tmp_path / "a.md", target_name="a",
        kind=ContradictionKind.METRIC_UPDATE, reason="r", confidence=0.9,
        new_body_excerpt="new ex", old_body_excerpt="old ex",
    )]
    out = append_to_review_queue("new-x", c, new_path=tmp_path / "new_x.md")

    assert out.exists()
    import json
    line = json.loads(out.read_text(encoding="utf-8").strip())
    assert line["new_slug"] == "new-x"
    assert line["new_path"].endswith("new_x.md")
    assert line["kind"] == "metric_update"
    assert line["resolved"] is False
    assert line["confidence"] == 0.9
    assert line["target_name"] == "a"


def test_append_to_review_queue_appends_multiple_calls(tmp_path, monkeypatch):
    """Multiple calls append to same file, don't overwrite."""
    from src.contradiction_detector import (
        append_to_review_queue, Contradiction, ContradictionKind,
    )
    monkeypatch.setenv("MV3_RUNTIME_DIR", str(tmp_path))

    c1 = [Contradiction(target_path=tmp_path/"a.md", target_name="a",
                        kind=ContradictionKind.METRIC_UPDATE, reason="r1", confidence=0.9)]
    c2 = [Contradiction(target_path=tmp_path/"b.md", target_name="b",
                        kind=ContradictionKind.FACT_CORRECTION, reason="r2", confidence=0.8)]
    append_to_review_queue("x", c1, new_path=tmp_path / "x.md")
    out = append_to_review_queue("y", c2, new_path=tmp_path / "y.md")

    lines = out.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 2
