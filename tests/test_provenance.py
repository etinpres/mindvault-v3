import importlib.util
from pathlib import Path


def _load(monkeypatch, tmp_path):
    monkeypatch.setenv("MV3_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MV3_MEMORY_DIR", str(tmp_path / "memory"))
    spec = importlib.util.spec_from_file_location(
        "sme", Path(__file__).parent.parent / "src" / "session_memory_end.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_write_staged_records_source(monkeypatch, tmp_path):
    sme = _load(monkeypatch, tmp_path)
    item = {"type": "project", "title": "t", "reason": "r",
            "evidence": "e", "body": "b"}
    path = sme.write_staged(item, session_id="abcd1234-5678-90ab-cdef-111122223333")
    assert path is not None
    text = path.read_text()
    assert "source_type: session" in text
    assert "source_ref: abcd1234-5678-90ab-cdef-111122223333" in text


def test_write_staged_records_explicit_source_override(monkeypatch, tmp_path):
    sme = _load(monkeypatch, tmp_path)
    item = {"type": "project", "title": "t", "reason": "r",
            "evidence": "e", "body": "b"}
    path = sme.write_staged(item, session_id="abcd1234-5678-90ab-cdef-111122223333",
                            source_type="url", source_ref="https://youtu.be/abc123")
    assert path is not None
    text = path.read_text()
    assert "source_type: url" in text
    assert "source_ref: https://youtu.be/abc123" in text
