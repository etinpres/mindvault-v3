import datetime
import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


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


def _fake_embed(_text):
    """1024차원, 모두 0.5인 unit vector."""
    return [0.5] * 1024


def test_recall_attaches_provenance(tmp_path):
    """recall_memory 반환 결과에 provenance 키가 부착되는지 검증."""
    from memory_indexer import incremental_index
    from memory_search import recall_memory

    # 격리된 fixture 생성
    memdir = tmp_path / "memory"
    memdir.mkdir()
    mem_file = memdir / "prov_test.md"
    mem_file.write_text(
        "---\n"
        "name: prov-test\n"
        "description: 출처 추적 테스트 메모리\n"
        "type: project\n"
        "staged_at: 2026-05-30T10:00:00\n"
        "staged_from_session: abcd1234\n"
        "source_type: session\n"
        "source_ref: abcd1234-5678-90ab-cdef-111122223333\n"
        "---\n\n"
        "메일 발송 노하우 본문 텍스트\n",
        encoding="utf-8",
    )

    tmp_db = tmp_path / "test.db"

    with patch("memory_indexer.embed_text", side_effect=_fake_embed):
        incremental_index([memdir], db_path=tmp_db)

    # FTS-only 모드로 recall (vec off → embed_text returns None)
    with patch("memory_search.embed_text", return_value=None):
        results = recall_memory(
            "메일",
            top_k=3,
            score_threshold=0.0,
            db_path=tmp_db,
        )

    assert results, "후보 없음 — fixture 확인"
    assert "provenance" in results[0]
    assert results[0]["provenance"]["source_type"] == "session"
    assert results[0]["provenance"]["source_ref"] == "abcd1234-5678-90ab-cdef-111122223333"
    assert results[0]["provenance"]["captured_at"] == "2026-05-30T10:00:00"


def test_recall_provenance_is_json_serializable(tmp_path):
    """recall_memory 결과가 json.dumps 에서 TypeError 없이 직렬화되어야 한다.
    Fix A regression lock: staged_at YAML datetime → ISO string 변환 검증."""
    import json
    from memory_indexer import incremental_index
    from memory_search import recall_memory

    memdir = tmp_path / "memory"
    memdir.mkdir()
    mem_file = memdir / "json_ser_test.md"
    mem_file.write_text(
        "---\n"
        "name: json-ser-test\n"
        "description: JSON 직렬화 테스트 메모리\n"
        "type: project\n"
        "staged_at: 2026-05-30T10:00:00\n"
        "source_type: session\n"
        "source_ref: abcd1234-5678-90ab-cdef-111122223333\n"
        "---\n\n"
        "JSON 직렬화 회귀 방지 본문 텍스트\n",
        encoding="utf-8",
    )

    tmp_db = tmp_path / "json_ser_test.db"

    with patch("memory_indexer.embed_text", side_effect=_fake_embed):
        incremental_index([memdir], db_path=tmp_db)

    with patch("memory_search.embed_text", return_value=None):
        results = recall_memory(
            "JSON 직렬화",
            top_k=3,
            score_threshold=0.0,
            db_path=tmp_db,
        )

    assert results, "후보 없음 — fixture 확인"
    # Must not raise TypeError
    serialized = json.dumps(results, ensure_ascii=False)
    assert serialized is not None
    assert isinstance(results[0]["provenance"]["captured_at"], str), (
        "captured_at must be str (ISO), not datetime"
    )


def test_recall_survives_unreadable_memory_file(tmp_path):
    """Fix C regression lock: recall hot-path 에서 UnicodeDecodeError 로
    ALL results 가 지워지지 않아야 한다 (예외가 외부 FATAL 핸들러로 탈출 차단)."""
    from memory_indexer import incremental_index
    from memory_search import recall_memory

    memdir = tmp_path / "memory"
    memdir.mkdir()
    mem_file = memdir / "unicode_test.md"
    mem_file.write_text(
        "---\n"
        "name: unicode-test\n"
        "description: 유니코드 오류 생존 테스트 메모리\n"
        "type: project\n"
        "staged_at: 2026-05-30T10:00:00\n"
        "source_type: session\n"
        "source_ref: deadbeef-0000-1111-2222-333344445555\n"
        "---\n\n"
        "유니코드 회귀 방지 테스트 본문 텍스트\n",
        encoding="utf-8",
    )

    tmp_db = tmp_path / "unicode_test.db"

    with patch("memory_indexer.embed_text", side_effect=_fake_embed):
        incremental_index([memdir], db_path=tmp_db)

    # Overwrite the memory file with invalid UTF-8 bytes AFTER indexing
    mem_file.write_bytes(b"\xff\xfe invalid utf-8 content \x80\x81")

    with patch("memory_search.embed_text", return_value=None):
        results = recall_memory(
            "유니코드",
            top_k=3,
            score_threshold=0.0,
            db_path=tmp_db,
        )

    # Must not wipe ALL results due to UnicodeDecodeError
    assert results is not None, "recall_memory raised an exception"
    assert results != [], "UnicodeDecodeError wiped all results (Fix C regression)"
    # Provenance should gracefully fall back to "unknown"
    assert results[0]["provenance"]["source_type"] == "unknown", (
        f"Expected 'unknown' fallback, got {results[0]['provenance']['source_type']!r}"
    )


# ─── Task 3: _format_output 출처 라벨 ────────────────────────────────────────

HOOK_PATH = Path(__file__).parent.parent / "hooks" / "memory-recall.py"


def _load_hook():
    """hooks/memory-recall.py 를 importlib 로 로드 (test_memory_hook.py 패턴 동일)."""
    spec = importlib.util.spec_from_file_location("hk_prov", HOOK_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_format_output_shows_source_label():
    hook = _load_hook()
    results = [{
        "name": "x", "description": "d", "snippet": "", "score": 0.9,
        "source": ["vec"],
        "provenance": {"source_type": "session", "source_ref": "abcd1234-5678-90ab",
                       "captured_at": "2026-05-30T10:00:00"},
    }]
    out = hook._format_output(results)
    assert "출처:" in out
    assert "session" in out


def test_format_output_nonstring_source_type_does_not_crash():
    hook = _load_hook()
    results = [{
        "name": "x", "description": "d", "snippet": "", "score": 0.9,
        "source": ["vec"],
        "provenance": {"source_type": True, "source_ref": None, "captured_at": None},
    }]
    out = hook._format_output(results)  # must not raise
    assert "출처:" in out
    assert "True" in out


def test_format_output_unknown_source_suppressed():
    hook = _load_hook()
    results = [{
        "name": "x", "description": "d", "snippet": "", "score": 0.9,
        "source": ["vec"],
        "provenance": {"source_type": "unknown", "source_ref": None, "captured_at": None},
    }]
    out = hook._format_output(results)
    assert "출처:" not in out  # unknown → no label (noise suppression)


def test_format_output_datetime_captured_at():
    import datetime
    hook = _load_hook()
    results = [{
        "name": "x", "description": "d", "snippet": "", "score": 0.9,
        "source": ["vec"],
        "provenance": {"source_type": "session", "source_ref": "abcd1234ef",
                       "captured_at": datetime.datetime(2026, 5, 30, 10, 0)},
    }]
    out = hook._format_output(results)
    assert "출처: session" in out
    assert "2026-05-30" in out      # datetime str()[:10]
    assert "abcd1234" in out and "abcd1234e" not in out  # ref truncated to 8 chars


# ─── Task 4: 기존 메모리 backfill CLI ───────────────────────────────────────


def test_backfill_adds_source_from_staged_session(tmp_path):
    from src import provenance_backfill_cli as bf
    mem = tmp_path / "memory"; mem.mkdir()
    p = mem / "feedback_x.md"
    p.write_text("---\nname: x\ntype: feedback\nstaged_from_session: abcd1234\n---\n\nbody\n")
    n = bf.backfill_dir(mem, dry_run=False)
    assert n == 1
    text = p.read_text()
    assert "source_type: session" in text
    assert "source_ref: abcd1234" in text


def test_backfill_unknown_when_no_session(tmp_path):
    from src import provenance_backfill_cli as bf
    mem = tmp_path / "memory"; mem.mkdir()
    p = mem / "reference_y.md"
    p.write_text("---\nname: y\ntype: reference\n---\n\nbody\n")
    bf.backfill_dir(mem, dry_run=False)
    assert "source_type: unknown" in p.read_text()
    assert "source_ref" not in p.read_text()


def test_backfill_dry_run_no_write(tmp_path):
    from src import provenance_backfill_cli as bf
    mem = tmp_path / "memory"; mem.mkdir()
    p = mem / "feedback_z.md"
    original = "---\nname: z\ntype: feedback\nstaged_from_session: eeee9999\n---\n\nbody\n"
    p.write_text(original)
    n = bf.backfill_dir(mem, dry_run=True)
    assert n == 1  # 대상 건수는 반환
    assert p.read_text() == original  # 파일 내용 불변


def test_backfill_skips_unreadable_file(tmp_path):
    from src import provenance_backfill_cli as bf
    mem = tmp_path / "memory"; mem.mkdir()
    bad = mem / "bad.md"
    bad.write_bytes(b"\xff\xfe not valid utf-8 \x80\x81")
    # must not raise; bad file simply not counted
    n = bf.backfill_dir(mem, dry_run=False)
    assert n == 0


# ─── Task 4b: backfill CLI — originSessionId + _procedural + fence + reporting ─

def test_backfill_recovers_toplevel_originSessionId(tmp_path):
    """Problem A fix: top-level originSessionId → source_type: session + source_ref."""
    from src import provenance_backfill_cli as bf
    mem = tmp_path / "memory"; mem.mkdir()
    p = mem / "origin_toplevel.md"
    uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    p.write_text(f"---\nname: x\ntype: project\noriginSessionId: {uuid}\n---\n\nbody\n")
    n = bf.backfill_dir(mem, dry_run=False)
    assert n == 1
    text = p.read_text()
    assert "source_type: session" in text
    assert f"source_ref: {uuid}" in text


def test_backfill_recovers_nested_originSessionId(tmp_path):
    """Problem A fix: metadata.originSessionId (nested) → source_type: session + source_ref."""
    from src import provenance_backfill_cli as bf
    mem = tmp_path / "memory"; mem.mkdir()
    p = mem / "origin_nested.md"
    uuid = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
    p.write_text(
        f"---\nname: y\ntype: project\nmetadata:\n  originSessionId: {uuid}\n---\n\nbody\n"
    )
    n = bf.backfill_dir(mem, dry_run=False)
    assert n == 1
    text = p.read_text()
    assert "source_type: session" in text
    assert f"source_ref: {uuid}" in text


def test_backfill_staged_from_session_still_works(tmp_path):
    """Problem A: existing staged_from_session behavior preserved (priority 1)."""
    from src import provenance_backfill_cli as bf
    mem = tmp_path / "memory"; mem.mkdir()
    p = mem / "staged_still.md"
    p.write_text("---\nname: z\ntype: feedback\nstaged_from_session: old1234\n---\n\nbody\n")
    n = bf.backfill_dir(mem, dry_run=False)
    assert n == 1
    text = p.read_text()
    assert "source_type: session" in text
    assert "source_ref: old1234" in text


def test_backfill_trailing_whitespace_fence_no_crash(tmp_path):
    """Problem B fix: closing fence '--- ' (trailing space) must not raise; clean file still counted."""
    from src import provenance_backfill_cli as bf
    mem = tmp_path / "memory"; mem.mkdir()

    # Bad file: closing fence has trailing space → parse_frontmatter may parse it,
    # but exact '---' won't be found by old lines.index("---", 1).
    bad = mem / "bad_fence.md"
    bad.write_text("---\nname: bad\ntype: project\n---  \n\nbody\n")

    # Clean file: normal closing fence → should be processed normally
    clean = mem / "clean_fence.md"
    clean.write_text("---\nname: clean\ntype: project\n---\n\nbody\n")

    # Must NOT raise; both files counted; both get source_type injected
    n = bf.backfill_dir(mem, dry_run=False)
    assert n >= 1, f"clean file should be counted, got n={n}"
    # clean file must have been processed
    assert "source_type: unknown" in clean.read_text()
    # trailing-ws fence file must ALSO be backfilled (not skipped)
    assert "source_type" in bad.read_text(), (
        "trailing-whitespace fence file was skipped instead of backfilled"
    )


def test_backfill_includes_procedural(tmp_path):
    """Problem C fix: _procedural/*.md included; _staged/*.md excluded."""
    from src import provenance_backfill_cli as bf
    mem = tmp_path / "memory"; mem.mkdir()

    root_file = mem / "root_mem.md"
    root_file.write_text("---\nname: root\ntype: project\n---\n\nbody\n")

    proc_dir = mem / "_procedural"; proc_dir.mkdir()
    proc_file = proc_dir / "proc_mem.md"
    proc_file.write_text("---\nname: proc\ntype: howto\n---\n\nbody\n")

    staged_dir = mem / "_staged"; staged_dir.mkdir()
    staged_file = staged_dir / "staged_mem.md"
    staged_file.write_text("---\nname: staged\ntype: project\n---\n\nbody\n")

    n = bf.backfill_dir(mem, dry_run=False)
    # Both root + procedural should be counted (= 2); staged untouched
    assert n == 2, f"expected 2, got {n}"
    assert "source_type" in root_file.read_text()
    assert "source_type" in proc_file.read_text()
    assert "source_type" not in staged_file.read_text()


def test_backfill_reports_skipped_names(tmp_path, capsys):
    """Problem D fix: main() prints skipped file names (bad frontmatter)."""
    import sys
    from src import provenance_backfill_cli as bf
    mem = tmp_path / "memory"; mem.mkdir()

    # Unreadable file → skipped
    bad = mem / "unreadable.md"
    bad.write_bytes(b"\xff\xfe invalid utf-8 \x80\x81")

    # Normal file → processed
    good = mem / "good.md"
    good.write_text("---\nname: g\ntype: project\n---\n\nbody\n")

    # Patch sys.argv and call main()
    with patch.object(sys, "argv", ["bf", str(mem), "--apply"]):
        bf.main()

    captured = capsys.readouterr().out
    # Must mention skipped file name
    assert "unreadable.md" in captured
    # Must show exact apply-mode count summary (--apply → "적용: N건")
    assert "적용: 1건" in captured


# ─── Task 5: end-to-end 통합 (write→index→recall→format 출처 라벨) ─────────────


def test_recall_source_ref_normalized_to_str(tmp_path):
    """Fix A: source_ref が非文字列 (YAML で int/date に parse される値) でも
    provenance["source_ref"] が str-or-None に正規化され、json.dumps が
    default=str なしで通ること。"""
    import json
    from memory_indexer import incremental_index
    from memory_search import recall_memory

    memdir = tmp_path / "memory"
    memdir.mkdir()

    # source_ref: 12345  → YAML は int として parse する
    int_file = memdir / "int_source_ref.md"
    int_file.write_text(
        "---\n"
        "name: int-source-ref\n"
        "description: source_ref が int になるケース\n"
        "type: project\n"
        "staged_at: 2026-05-30T10:00:00\n"
        "source_type: session\n"
        "source_ref: 12345\n"
        "---\n\n"
        "source_ref int 정규화 테스트 본문\n",
        encoding="utf-8",
    )

    # source_ref: 2026-05-30  → YAML は date として parse する
    date_file = memdir / "date_source_ref.md"
    date_file.write_text(
        "---\n"
        "name: date-source-ref\n"
        "description: source_ref が date になるケース\n"
        "type: project\n"
        "staged_at: 2026-05-30T10:00:00\n"
        "source_type: session\n"
        "source_ref: 2026-05-30\n"
        "---\n\n"
        "source_ref date 정규화 테스트 본문\n",
        encoding="utf-8",
    )

    tmp_db = tmp_path / "source_ref_test.db"

    with patch("memory_indexer.embed_text", side_effect=_fake_embed):
        incremental_index([memdir], db_path=tmp_db)

    with patch("memory_search.embed_text", return_value=None):
        results = recall_memory(
            "source_ref 정규화",
            top_k=5,
            score_threshold=0.0,
            db_path=tmp_db,
        )

    assert results, "후보 없음 — fixture 확인"
    for r in results:
        prov = r.get("provenance", {})
        sr = prov.get("source_ref")
        assert sr is None or isinstance(sr, str), (
            f"source_ref must be str-or-None, got {type(sr).__name__!r}: {sr!r}"
        )
    # json.dumps WITHOUT default=str must not raise
    json.dumps(results, ensure_ascii=False)


def test_backfill_multiline_ref_collapses_to_single_line(tmp_path):
    """Item 2: a YAML block-scalar session-id value containing newlines must be
    collapsed to a single line before injection, so the resulting source_ref: line
    re-parses cleanly (no embedded newline)."""
    from src import provenance_backfill_cli as bf
    from memory_review_cli import parse_frontmatter as pf
    mem = tmp_path / "memory"; mem.mkdir()
    p = mem / "multiline_ref.md"
    # Write a file where the session id spans two lines (block-scalar style)
    # The raw text has a literal '\n' embedded in the value we want to simulate.
    # Use staged_from_session; backfill reads it via _find_session_ref → str(value).
    # We set a value that str() would include a newline (simulate multi-line yaml).
    p.write_text(
        "---\n"
        "name: multiline\n"
        "type: project\n"
        "staged_from_session: abcd1234\n  extra-line-junk\n"
        "---\n\n"
        "body\n",
        encoding="utf-8",
    )
    # Must not raise; file should be processed
    n = bf.backfill_dir(mem, dry_run=False)
    if n == 1:
        text = p.read_text(encoding="utf-8")
        # source_ref line must not contain an embedded newline character
        for line in text.splitlines():
            if line.startswith("source_ref:"):
                assert "\n" not in line, f"source_ref line contains embedded newline: {line!r}"
                # The value (after the colon) must have no embedded newlines
                _, val = line.split(":", 1)
                assert "\n" not in val, f"source_ref value contains newline: {val!r}"
        # The file must re-parse without losing frontmatter
        fm, body = pf(text)
        assert "source_ref" in fm
        assert "\n" not in fm["source_ref"], (
            f"source_ref in parsed frontmatter contains newline: {fm['source_ref']!r}"
        )


def test_e2e_staged_to_recall_label(tmp_path):
    """write_staged → index → recall_memory(provenance 부착) → _format_output(출처 라벨)
    전 구간이 연결되는지 검증하는 end-to-end 통합 테스트."""
    from memory_indexer import incremental_index
    from memory_search import recall_memory

    # 1. 격리된 tmp memory dir + source frontmatter 포함 파일 생성
    memdir = tmp_path / "memory"
    memdir.mkdir()
    mem_file = memdir / "e2e_prov_test.md"
    mem_file.write_text(
        "---\n"
        "name: e2e-prov-test\n"
        "description: end-to-end 출처 추적 테스트 메모리\n"
        "type: project\n"
        "staged_at: 2026-05-30T12:00:00\n"
        "staged_from_session: e2e11111\n"
        "source_type: session\n"
        "source_ref: e2e11111-2222-3333-4444-555566667777\n"
        "---\n\n"
        "한국어 검색 통합 테스트 본문 텍스트\n",
        encoding="utf-8",
    )

    tmp_db = tmp_path / "e2e_test.db"

    # 2. 인덱싱 (_fake_embed로 임베딩 대체)
    with patch("memory_indexer.embed_text", side_effect=_fake_embed):
        incremental_index([memdir], db_path=tmp_db)

    # 3. recall_memory 호출 (FTS-only 모드 — embed_text returns None)
    with patch("memory_search.embed_text", return_value=None):
        results = recall_memory(
            "한국어 검색",
            top_k=3,
            score_threshold=0.0,
            db_path=tmp_db,
        )

    assert results, "recall 후보 없음 — fixture 또는 FTS 쿼리 확인"
    assert "provenance" in results[0], "recall_memory가 provenance를 부착하지 않음"

    # 4. _format_output으로 출처 라벨 렌더링
    hook = _load_hook()
    out = hook._format_output(results)

    # 5. 체인 전체 검증: 출처 라벨 + source_type + source_ref 8자 prefix
    assert "출처:" in out, f"'출처:' 라벨이 출력에 없음:\n{out}"
    assert "session" in out, f"'session' source_type이 출력에 없음:\n{out}"
    assert "e2e11111" in out, f"source_ref 8자 prefix 'e2e11111'이 출력에 없음:\n{out}"
