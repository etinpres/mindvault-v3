"""T8 — Layer 4 hook deprecated_by score decay tests.

Load hooks/memory-recall.py via importlib.util.spec_from_file_location
since the hyphen in the filename prevents normal import.
"""
from __future__ import annotations
import importlib.util
import pytest
from pathlib import Path


@pytest.fixture
def memory_recall_mod():
    """Load hooks/memory-recall.py as 'memory_recall_mod'."""
    repo_root = Path(__file__).resolve().parent.parent
    src = repo_root / "hooks" / "memory-recall.py"
    spec = importlib.util.spec_from_file_location("memory_recall_mod", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_is_deprecated_detects_frontmatter(tmp_path, memory_recall_mod):
    dep = tmp_path / "a.md"
    dep.write_text(
        "---\nname: a\ndeprecated_by: [b]\ntype: feedback\n---\n\nbody\n",
        encoding="utf-8",
    )
    fresh = tmp_path / "b.md"
    fresh.write_text("---\nname: b\ntype: feedback\n---\n\nbody\n", encoding="utf-8")

    assert memory_recall_mod._is_deprecated(dep) is True
    assert memory_recall_mod._is_deprecated(fresh) is False


def test_is_deprecated_handles_missing_file(memory_recall_mod, tmp_path):
    """Non-existent file → False (no crash)."""
    missing = tmp_path / "does-not-exist.md"
    assert memory_recall_mod._is_deprecated(missing) is False


def test_is_deprecated_handles_no_frontmatter(tmp_path, memory_recall_mod):
    """File without frontmatter → False."""
    plain = tmp_path / "plain.md"
    plain.write_text("just markdown, no frontmatter", encoding="utf-8")
    assert memory_recall_mod._is_deprecated(plain) is False


def test_apply_deprecation_decay_multiplies_score(tmp_path, memory_recall_mod):
    dep = tmp_path / "a.md"
    dep.write_text(
        "---\nname: a\ndeprecated_by: [b]\n---\n\nbody\n",
        encoding="utf-8",
    )
    result = memory_recall_mod._apply_deprecation_decay(dep, original_score=0.85)
    expected = 0.85 * memory_recall_mod.DEPRECATED_DECAY
    assert abs(result - expected) < 1e-6


def test_apply_deprecation_decay_passthrough_non_deprecated(tmp_path, memory_recall_mod):
    fresh = tmp_path / "b.md"
    fresh.write_text("---\nname: b\n---\n\nbody\n", encoding="utf-8")
    assert memory_recall_mod._apply_deprecation_decay(fresh, 0.85) == 0.85


def test_apply_deprecation_decay_handles_zero_score(tmp_path, memory_recall_mod):
    dep = tmp_path / "a.md"
    dep.write_text("---\nname: a\ndeprecated_by: [b]\n---\n", encoding="utf-8")
    # 0 * 0.3 = 0
    assert memory_recall_mod._apply_deprecation_decay(dep, 0.0) == 0.0


def test_deprecated_decay_constant_is_03(memory_recall_mod):
    """DEPRECATED_DECAY = 0.3 (T8 spec)."""
    assert memory_recall_mod.DEPRECATED_DECAY == 0.3


def test_is_deprecated_matches_block_style_yaml(tmp_path, memory_recall_mod):
    """deprecated_by:\\n  - foo (block style) must also be detected."""
    dep = tmp_path / "a.md"
    dep.write_text(
        "---\nname: a\ndeprecated_by:\n  - foo\n  - bar\ntype: feedback\n---\n\nbody\n",
        encoding="utf-8",
    )
    assert memory_recall_mod._is_deprecated(dep) is True


def test_is_deprecated_handles_crlf(tmp_path, memory_recall_mod):
    """CRLF-saved memory (manual Windows/Obsidian edit) must still detect frontmatter."""
    dep = tmp_path / "a.md"
    dep.write_bytes(
        "---\r\nname: a\r\ndeprecated_by: [b]\r\n---\r\n\r\nbody\r\n".encode("utf-8")
    )
    assert memory_recall_mod._is_deprecated(dep) is True


def test_is_deprecated_handles_crlf_block_style(tmp_path, memory_recall_mod):
    """CRLF + block-style deprecated_by list must also be detected."""
    dep = tmp_path / "a.md"
    dep.write_bytes(
        "---\r\nname: a\r\ndeprecated_by:\r\n  - b\r\ntype: feedback\r\n---\r\n\r\nbody\r\n".encode("utf-8")
    )
    assert memory_recall_mod._is_deprecated(dep) is True


def test_is_deprecated_ignores_deprecated_by_in_body(tmp_path, memory_recall_mod):
    """body 안 'deprecated_by: [...]' literal must NOT trigger detection (frontmatter only)."""
    plain = tmp_path / "a.md"
    plain.write_text(
        "---\nname: a\ntype: feedback\n---\n\n"
        "이 항목은 deprecated_by: [legacy] 라는 키워드는 무시되어야 함.\n",
        encoding="utf-8",
    )
    assert memory_recall_mod._is_deprecated(plain) is False


def test_apply_decay_also_decays_raw_cosine_via_recall_pipeline(tmp_path, monkeypatch):
    """Integration: actual recall_memory ranks fresh ABOVE deprecated.

    Mocks _fts_top_k + _vec_top_k to return controlled raw_cosine scores,
    so we can assert ordering changes after T8 decay.
    """
    import sys
    repo_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo_root / "src"))
    from memory_search import recall_memory
    import memory_search as ms

    # Build two real files for _is_deprecated to read
    mem = tmp_path / "memory"
    mem.mkdir()
    dep = mem / "feedback_deprecated.md"
    fresh = mem / "feedback_fresh.md"
    dep.write_text(
        "---\nname: deprecated\ndeprecated_by: [fresh]\n---\nold body\n",
        encoding="utf-8",
    )
    fresh.write_text(
        "---\nname: fresh\n---\nnew body\n", encoding="utf-8"
    )

    # Stub the index DB lookups: deprecated has raw=0.78, fresh has raw=0.74
    # _vec_top_k returns (rows, raw_cosine_map)
    def fake_vec(conn, qvec, limit, use_ctx=False):  # use_ctx: Phase 5 CR 경로 시그니처
        rows = [
            (str(dep), 1, "0.78"),
            (str(fresh), 2, "0.74"),
        ]
        cosines = {str(dep): 0.78, str(fresh): 0.74}
        return rows, cosines

    # _fts_top_k returns rows
    def fake_fts(conn, query, limit):
        return [(str(dep), 1, "0.5"), (str(fresh), 2, "0.5")]

    # embed_text returns dummy vec
    monkeypatch.setattr(ms, "embed_text", lambda q: [0.0] * 10)
    monkeypatch.setattr(ms, "_vec_top_k", fake_vec)
    monkeypatch.setattr(ms, "_fts_top_k", fake_fts)
    monkeypatch.setattr(ms, "_expand_wikilinks", lambda *a, **k: [])
    monkeypatch.setattr(ms, "_alias_boost_paths", lambda q: set())

    # FakeConn must support execute() for the meta lookup loop.
    class FakeRow(dict):
        def __getitem__(self, k):
            return super().__getitem__(k) if k in self else ""

    class FakeCursor:
        def __init__(self, path):
            self._path = path

        def fetchone(self):
            # Return a dict-like row with name/description fields.
            name = "deprecated" if "deprecated" in self._path else "fresh"
            return FakeRow(name=name, description="")

    class FakeConn:
        def execute(self, sql, params=()):
            # Only the meta lookup uses execute in this path.
            path = params[0] if params else ""
            return FakeCursor(path)

        def close(self):
            pass

    monkeypatch.setattr(ms, "open_db", lambda path: FakeConn())
    monkeypatch.setattr(ms, "_snippet", lambda c, p, query=None: "snippet")
    monkeypatch.setattr(ms, "_resolve_wikilink", lambda c, s: None)

    fake_db = tmp_path / "fake.db"
    fake_db.write_text("")  # is_file() check

    results = recall_memory(
        "test query",
        top_k=2,
        score_threshold=0.0,
        db_path=fake_db,
        raw_cosine_min=0.3,
        expand_wikilinks=False,
    )

    # Must return at least one result, and the FIRST must be fresh (not deprecated)
    assert len(results) >= 1, f"expected at least one result, got {results}"
    top_path = results[0]["path"]
    assert "fresh" in top_path, (
        f"After T8 decay, fresh memory should outrank deprecated. "
        f"Got top={top_path}. All results: {[r['path'] for r in results]}"
    )


def test_is_deprecated_finds_fence_past_2048_chars(tmp_path):
    """audit R3: frontmatter 가 2048자를 넘어도(Phase1 ①/③ 주입 후) closing fence 를
    검출해 deprecated 감쇠가 유지된다(옛 2KB cap 은 fence 못 찾아 감쇠 silent 누락)."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from memory_search import _is_deprecated
    p = tmp_path / "dep.md"
    pad = "x" * 2100  # description 을 길게 — closing fence + deprecated_by 가 2048자 밖
    p.write_text(
        f"---\nname: m\ndescription: {pad}\ndeprecated_by: [newer-memory]\n---\n\nbody\n",
        encoding="utf-8",
    )
    assert _is_deprecated(p) is True   # 2KB 밖 fence/deprecated_by 도 검출


def test_is_deprecated_bom_tolerant(tmp_path):
    """audit R3: 선행 BOM 메모리도 deprecated 검출(parser 정렬)."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from memory_search import _is_deprecated
    p = tmp_path / "dep_bom.md"
    p.write_text("﻿---\nname: m\ndeprecated_by: [newer]\n---\n\nbody\n", encoding="utf-8")
    assert _is_deprecated(p) is True
