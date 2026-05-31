"""Phase 1③ 신뢰성 검증 — stale 자동 감지 테스트."""
from reverify import (
    CanonicalFact,
    CANONICAL_FACTS,
    check_memory_staleness,
    verify_registry,
    default_root,
)


def _fake_root(tmp_path):
    """현행 코드 ground truth 모사: arctic 라이브, 8081 라이브, bge 없음."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "memory_indexer.py").write_text(
        'EMBED_URL = "http://localhost:8081/embed"\n# Arctic-Embed-L v2.0 KO\n',
        encoding="utf-8",
    )
    return tmp_path


# --- 핵심 판별 신호 (설계 §2): injected root 로 결정론 ---
def test_stale_when_alias_present_and_current_absent(tmp_path):
    root = _fake_root(tmp_path)
    v = check_memory_staleness("BGE-M3 임베딩이 형 메시지 어디든 0.7+ 매칭한다.", root)
    assert v.status == "stale"
    assert "embedding_model" in v.note


def test_fresh_history_when_both_tokens_present(tmp_path):
    root = _fake_root(tmp_path)
    v = check_memory_staleness("임베딩: arctic-ko v2.0 (Sprint 9 BGE-M3 → 교체)", root)
    assert v.status == "fresh"


def test_fresh_when_only_current_token(tmp_path):
    root = _fake_root(tmp_path)
    v = check_memory_staleness("arctic-ko 8081 정상 동작 확인.", root)
    assert v.status == "fresh"


def test_fresh_when_no_canonical_tokens(tmp_path):
    root = _fake_root(tmp_path)
    v = check_memory_staleness("오늘 카드뉴스 4건 렌더 완료.", root)
    assert v.status == "fresh"


def test_stale_port_8765(tmp_path):
    root = _fake_root(tmp_path)
    v = check_memory_staleness("Arctic-ko 포트는 8765 입니다.", root)
    # 'Arctic-ko' 가 embedding_model current(arctic) 동반이라 embedding_model 면제,
    # 단 embedding_port fact 는 current=8081 미언급이라 stale.
    assert v.status == "stale"
    assert "embedding_port" in v.note


def test_port_8081_not_matched_inside_18081(tmp_path):
    root = _fake_root(tmp_path)
    # 18081 안의 8081 이 current 토큰으로 오매칭되면 안 됨 (word-boundary)
    v = check_memory_staleness("eval 서버는 18081 (BGE-M3 별도 spin-up).", root)
    # current 8081 미포함(18081 은 boundary 로 불일치) + 8765 미포함 → embedding_port no-op
    # bge-m3 포함 + arctic 미포함 → embedding_model stale
    assert v.status == "stale"
    assert "embedding_model" in v.note


def test_verifier_fail_skips_fact(tmp_path):
    # arctic 이 라이브에 없는 root → embedding_model verifier False → 그 fact 판정 skip
    src = tmp_path / "src"
    src.mkdir()
    (src / "memory_indexer.py").write_text("EMBED_URL = nothing here\n", encoding="utf-8")
    v = check_memory_staleness("BGE-M3 임베딩 사용", tmp_path)
    # embedding_model verifier fail → skip, embedding_port verifier 도 8081 없음 → skip
    assert v.status == "fresh"  # 판정 불가 fact 는 stale 로 몰지 않음


# --- 레지스트리 self-check: 실제 repo 코드에서 모든 verifier 통과 (registry 정직성) ---
def test_verify_registry_all_live_on_real_repo():
    """CANONICAL_FACTS 의 모든 current_value 가 라이브 코드에 실재해야 한다.
    실패 = 코드가 또 바뀌었는데 registry 미갱신 (registry stale)."""
    failed = verify_registry(default_root())
    assert failed == [], f"registry stale — verifier fail: {failed}"


def test_current_value_not_matched_as_substring(tmp_path):
    """current_value 'arctic' 가 'subarctic' 안에서 오매칭되어 stale 을 잘못 면제하면 안 됨."""
    root = _fake_root(tmp_path)
    v = check_memory_staleness("The subarctic region still uses BGE-M3.", root)
    assert v.status == "stale"           # subarctic ≠ arctic → 면제 안 됨 → stale
    assert "embedding_model" in v.note


def test_token_matches_adjacent_korean(tmp_path):
    """'arctic임베딩'(한국어 바로 붙음)도 arctic 포함으로 인정 (면제)."""
    root = _fake_root(tmp_path)
    v = check_memory_staleness("arctic임베딩 사용 중, 예전 BGE-M3 표기.", root)
    assert v.status == "fresh"           # arctic 동반 → embedding_model 면제


from reverify import (
    upsert_reverify_frontmatter,
    write_back_verdict,
    scan_memories,
    maybe_scan_due,
)
from reverify import StaleVerdict as _SV  # noqa


# --- upsert 순수 함수 ---
def test_upsert_adds_keys_no_frontmatter():
    from reverify import _current_reverify_note
    out = upsert_reverify_frontmatter("본문만 있음", "stale", "n1", "2026-05-31")
    assert out.startswith("---\n")
    assert "reverify_status: stale" in out
    assert _current_reverify_note(out) == "n1"   # JSON 인용돼 저장, 디코드 시 원본
    assert "본문만 있음" in out


def test_upsert_preserves_body_and_existing_keys():
    text = "---\nname: m\ntype: feedback\n---\n\n본문 줄1\n본문 줄2\n"
    out = upsert_reverify_frontmatter(text, "stale", "note", "2026-05-31")
    assert "name: m" in out and "type: feedback" in out
    assert "본문 줄1" in out and "본문 줄2" in out
    assert "reverify_status: stale" in out


def test_upsert_replaces_existing_reverify_keys():
    from reverify import _current_reverify_note
    text = "---\nname: m\nreverify_status: stale\nreverify_note: old\nreverify_checked: 2026-01-01\n---\n\nbody\n"
    out = upsert_reverify_frontmatter(text, "stale", "new", "2026-05-31")
    assert out.count("reverify_status:") == 1
    assert _current_reverify_note(out) == "new"
    assert "old" not in out


def test_upsert_note_oneline():
    from reverify import _current_reverify_note
    out = upsert_reverify_frontmatter("body", "stale", "줄1\n줄2", "2026-05-31")
    assert _current_reverify_note(out) == "줄1 줄2"   # 단일 라인 정규화 + 디코드
    assert out.count("reverify_note:") == 1


# --- write-back: atomic, idempotent, cleanup ---
def test_write_back_flags_stale(tmp_path):
    p = tmp_path / "m.md"
    p.write_text("---\nname: m\n---\n\nBGE-M3 임베딩\n", encoding="utf-8")
    wrote = write_back_verdict(p, _SV(status="stale", note="x"), "2026-05-31")
    assert wrote is True
    assert "reverify_status: stale" in p.read_text(encoding="utf-8")


def test_write_back_idempotent(tmp_path):
    p = tmp_path / "m.md"
    p.write_text("---\nname: m\n---\n\nBGE-M3\n", encoding="utf-8")
    write_back_verdict(p, _SV(status="stale", note="x"), "2026-05-31")
    first = p.read_text(encoding="utf-8")
    wrote2 = write_back_verdict(p, _SV(status="stale", note="x"), "2026-06-09")  # 날짜 달라도
    assert wrote2 is False                       # status/note 불변 → no-write
    assert p.read_text(encoding="utf-8") == first  # checked churn 없음


def test_write_back_cleans_up_when_fresh(tmp_path):
    p = tmp_path / "m.md"
    p.write_text(
        "---\nname: m\nreverify_status: stale\nreverify_note: x\nreverify_checked: 2026-05-31\n---\n\nbody\n",
        encoding="utf-8",
    )
    wrote = write_back_verdict(p, _SV(status="fresh"), "2026-06-09")
    assert wrote is True
    txt = p.read_text(encoding="utf-8")
    assert "reverify_status" not in txt   # stale→fresh 전이 시 키 제거
    assert "name: m" in txt and "body" in txt


def test_write_back_noop_when_fresh_and_no_flag(tmp_path):
    p = tmp_path / "m.md"
    orig = "---\nname: m\n---\n\narctic-ko 정상\n"
    p.write_text(orig, encoding="utf-8")
    wrote = write_back_verdict(p, _SV(status="fresh"), "2026-06-09")
    assert wrote is False
    assert p.read_text(encoding="utf-8") == orig  # fresh 메모리 무손상


# --- scan_memories + sidecar ---
def _fake_root_for_scan(tmp_path):
    src = tmp_path / "code" / "src"
    src.mkdir(parents=True)
    (src / "memory_indexer.py").write_text(
        'EMBED_URL = "http://localhost:8081/embed"\n# Arctic\n', encoding="utf-8"
    )
    return tmp_path / "code"


def test_scan_flags_only_stale(tmp_path, monkeypatch):
    root = _fake_root_for_scan(tmp_path)
    mem = tmp_path / "mem"
    mem.mkdir()
    (mem / "stale.md").write_text("---\nname: s\n---\n\nBGE-M3 임베딩\n", encoding="utf-8")
    (mem / "fresh.md").write_text("---\nname: f\n---\n\narctic-ko 동작\n", encoding="utf-8")
    (mem / "hist.md").write_text("---\nname: h\n---\n\nBGE-M3 → arctic 교체\n", encoding="utf-8")
    (mem / "MEMORY.md").write_text("index\n", encoding="utf-8")
    monkeypatch.setenv("MV3_DATA_DIR", str(tmp_path / "data"))
    stats = scan_memories(mem, root=root, checked="2026-05-31")
    assert stats["flagged"] == 1
    assert "reverify_status: stale" in (mem / "stale.md").read_text(encoding="utf-8")
    assert "reverify_status" not in (mem / "fresh.md").read_text(encoding="utf-8")
    assert "reverify_status" not in (mem / "hist.md").read_text(encoding="utf-8")


def test_maybe_scan_due_first_run_then_skips(tmp_path, monkeypatch):
    root = _fake_root_for_scan(tmp_path)
    mem = tmp_path / "mem"
    mem.mkdir()
    (mem / "s.md").write_text("---\nname: s\n---\n\nBGE-M3\n", encoding="utf-8")
    monkeypatch.setenv("MV3_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MV3_REVERIFY_ROOT", str(root))
    s1 = maybe_scan_due(mem, interval_days=7)
    assert s1 is not None and s1["flagged"] == 1   # 첫 실행 → scan
    s2 = maybe_scan_due(mem, interval_days=7)
    assert s2 is None                               # sidecar 최신 → skip


def test_upsert_yaml_roundtrip_note_with_real_finding(tmp_path):
    """scan 이 만든 note 가 yaml.safe_load 로 round-trip 돼야 한다 (recall_memory 가
    parse_frontmatter→yaml.safe_load 로 reverify_status 를 읽으므로 깨지면 라벨 미렌더)."""
    import yaml
    from reverify import _FM_RE
    root = _fake_root(tmp_path)
    note = check_memory_staleness("BGE-M3 임베딩이 0.7+ 매칭", root).note
    out = upsert_reverify_frontmatter("---\nname: m\n---\n\nbody\n", "stale", note, "2026-05-31")
    m = _FM_RE.match(out)
    d = yaml.safe_load(m.group(1))          # 깨지면 여기서 예외/None
    assert d["reverify_status"] == "stale"
    assert "bge-m3" in d["reverify_note"]


def test_scanned_file_frontmatter_yaml_parseable(tmp_path, monkeypatch):
    """scan 후 실제 파일이 yaml 파싱 가능 (recall_memory 경로 보장)."""
    import yaml
    from reverify import _FM_RE
    root = _fake_root(tmp_path)
    monkeypatch.setenv("MV3_DATA_DIR", str(tmp_path / "data"))
    mem = tmp_path / "mem"
    mem.mkdir()
    p = mem / "s.md"
    p.write_text("---\nname: s\ntype: feedback\n---\n\nBGE-M3 임베딩\n", encoding="utf-8")
    scan_memories(mem, root=root, checked="2026-05-31")
    m = _FM_RE.match(p.read_text(encoding="utf-8"))
    d = yaml.safe_load(m.group(1))
    assert d["reverify_status"] == "stale"
    assert d["name"] == "s" and d["type"] == "feedback"   # 기존 키 보존 + 파싱 정상


def test_collect_includes_procedural(tmp_path):
    from reverify import _collect_memory_files
    mem = tmp_path / "mem"
    (mem / "_procedural").mkdir(parents=True)
    (mem / "a.md").write_text("x", encoding="utf-8")
    (mem / "_procedural" / "b.md").write_text("y", encoding="utf-8")
    (mem / "MEMORY.md").write_text("idx", encoding="utf-8")
    names = {p.name for p in _collect_memory_files(mem)}
    assert names == {"a.md", "b.md"}   # MEMORY.md 제외, _procedural 포함


def test_e2e_bge_memory_scan_to_recall_label(tmp_path, monkeypatch):
    """완료 게이트 e2e: BGE 주장 메모리 → scan flag → parse_frontmatter(yaml) 로 읽혀
    → 회수 포맷터가 경고 라벨 렌더. (전체 chain: scan→yaml→formatter)"""
    import recall_core
    from memory_indexer import parse_frontmatter
    root = _fake_root_for_scan(tmp_path)
    mem = tmp_path / "mem"
    mem.mkdir()
    bge = mem / "feedback_no_v1_token_waste.md"
    bge.write_text("---\nname: no-v1-token-waste\ntype: feedback\n---\n\nBGE-M3 임베딩이 0.7+ 매칭.\n", encoding="utf-8")
    monkeypatch.setenv("MV3_DATA_DIR", str(tmp_path / "data"))
    scan_memories(mem, root=root, checked="2026-05-31")

    # (1) scan 이 yaml-safe 하게 flag 했는가 — parse_frontmatter(yaml.safe_load) 로 읽힘
    fm, _ = parse_frontmatter(bge.read_text(encoding="utf-8"))
    assert fm.get("reverify_status") == "stale"
    assert fm.get("name") == "no-v1-token-waste"   # 기존 키 보존 + yaml 정상
    assert "bge-m3" in (fm.get("reverify_note") or "")

    # (2) recall_memory 가 부착하는 것과 동일 shape 로 포맷터에 넘기면 경고 라벨
    sample = [{
        "name": fm["name"], "source": ["vec"], "description": "d", "snippet": "",
        "score": 0.7,
        "reverify": {"status": fm["reverify_status"], "note": fm["reverify_note"]},
    }]
    out = recall_core.format_memory_context(sample, wrap_system_reminder=True)
    assert "재검증 필요:" in out and "bge-m3" in out


def test_maybe_scan_due_missing_dir_is_safe(tmp_path, monkeypatch):
    """존재하지 않는 mem_dir 여도 maybe_scan_due 가 예외 없이 안전 (best-effort)."""
    monkeypatch.setenv("MV3_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("MV3_REVERIFY_ROOT", str(_fake_root_for_scan(tmp_path)))
    stats = maybe_scan_due(tmp_path / "does_not_exist", interval_days=7)
    assert stats is not None
    assert stats["flagged"] == 0 and stats["total"] == 0


def test_session_end_wires_reverify_best_effort():
    """SessionEnd main() 이 reverify 를 best-effort(try/except)로 호출하도록 배선됨."""
    import inspect
    import session_memory_end
    src = inspect.getsource(session_memory_end.main)
    assert "maybe_scan_due" in src           # reverify 트리거 배선
    assert "reverify skipped" in src         # silent-fail _debug 마커


# ---- adversarial audit fixes ----
def test_scan_no_oscillation(tmp_path, monkeypatch):
    """CRITICAL: stale flag 후 재scan 해도 stale 유지 (reverify_note 의 'arctic' 이
    self-poison 해 면제→strip→재flag 진동하면 안 됨)."""
    root = _fake_root_for_scan(tmp_path)
    mem = tmp_path / "mem"
    mem.mkdir()
    p = mem / "s.md"
    p.write_text("---\nname: s\n---\n\nBGE-M3 임베딩 사용.\n", encoding="utf-8")
    monkeypatch.setenv("MV3_DATA_DIR", str(tmp_path / "data"))
    s1 = scan_memories(mem, root=root, checked="2026-05-31")
    assert s1["flagged"] == 1
    assert "reverify_status: stale" in p.read_text(encoding="utf-8")
    # 2회차: 진동 없이 stale 유지, 재기록 없음(idempotent)
    s2 = scan_memories(mem, root=root, checked="2026-06-09")
    assert "reverify_status: stale" in p.read_text(encoding="utf-8")  # 여전히 stale
    assert s2["cleared"] == 0                                          # strip 안 됨
    # 3회차도 동일
    scan_memories(mem, root=root, checked="2026-06-16")
    assert "reverify_status: stale" in p.read_text(encoding="utf-8")


def test_upsert_preserves_blank_line_separator():
    """BUG1: '---\\n...\\n---\\n\\nBody' 의 본문 구분 빈 줄이 upsert 후에도 보존."""
    text = "---\nname: x\n---\n\nBody content.\n"
    out = upsert_reverify_frontmatter(text, "stale", "n", "2026-05-31")
    assert "\n---\n\nBody content.\n" in out   # 빈 줄(구분자) 보존
    # strip 후 원복 (reverify 키만 제거, 빈 줄 유지)
    from reverify import _strip_reverify_frontmatter
    back = _strip_reverify_frontmatter(out)
    assert "\n---\n\nBody content.\n" in back


def test_upsert_bom_file_not_corrupted(tmp_path):
    """BUG3: BOM 접두 파일도 frontmatter 인식 → 이중 FM 생성/이름 소실 없음."""
    import yaml
    from reverify import _FM_RE
    text = "﻿---\nname: bom\ntype: feedback\n---\n\nBody.\n"
    out = upsert_reverify_frontmatter(text, "stale", "n", "2026-05-31")
    # 단일 frontmatter 블록만 (이중 아님): '---' 펜스가 정확히 2개
    assert out.count("\n---\n") <= 2 and out.split("---", 2)[1].count("name: bom") == 1
    m = _FM_RE.match(out)
    assert m is not None
    d = yaml.safe_load(m.group(1))
    assert d.get("name") == "bom" and d.get("reverify_status") == "stale"


def test_write_back_skips_file_without_frontmatter(tmp_path):
    """no-frontmatter 파일에 stale 플래그를 prepend 하지 않음 (안전 — 이중/오손상 방지)."""
    p = tmp_path / "nofm.md"
    orig = "그냥 본문, BGE-M3 언급.\n"   # frontmatter 없음
    p.write_text(orig, encoding="utf-8")
    from reverify import StaleVerdict as SV
    wrote = write_back_verdict(p, SV(status="stale", note="n"), "2026-05-31")
    assert wrote is False
    assert p.read_text(encoding="utf-8") == orig   # 무손상


def test_check_none_text_safe(tmp_path):
    root = _fake_root(tmp_path)
    assert check_memory_staleness(None, root).status == "fresh"
    assert check_memory_staleness("", root).status == "fresh"


def test_contains_empty_token_false():
    from reverify import _contains_token
    assert _contains_token("anything", "") is False


def test_grep_present_flat_layout(tmp_path):
    """배포 flat layout (memory_indexer.py 가 root 바로 아래) 도 verifier 통과."""
    from reverify import _grep_present
    (tmp_path / "memory_indexer.py").write_text("EMBED_URL=...8081...\n# Arctic\n", encoding="utf-8")
    assert _grep_present(tmp_path, "src/memory_indexer.py", r"arctic") is True   # basename 폴백


# ---- sweep round-1 fixes ----
def test_reverify_note_with_colon_yaml_safe(tmp_path):
    """audit: 확장 fact alias 에 ': ' 가 있어도 reverify_note 가 yaml.safe_load 로
    round-trip (frontmatter 전체 소실 차단). 라인 reader 도 디코드 일치(idempotent)."""
    import yaml
    from reverify import upsert_reverify_frontmatter, _FM_RE, _current_reverify_note
    note = "embedding_model 현재형 참조 mode: legacy (현행 arctic 미언급)"  # ': ' 위험류
    out = upsert_reverify_frontmatter("---\nname: m\n---\n\nbody\n", "stale", note, "2026-05-31")
    d = yaml.safe_load(_FM_RE.match(out).group(1))     # 안 깨져야(YAMLError 없음)
    assert d["reverify_status"] == "stale"
    assert d["reverify_note"] == note                   # yaml 디코드 == 원본
    assert _current_reverify_note(out) == note          # 라인 reader 디코드 == 원본


def test_reverify_note_hash_yaml_safe(tmp_path):
    """선행 '#' 토큰도 truncate 되지 않고 round-trip."""
    import yaml
    from reverify import upsert_reverify_frontmatter, _FM_RE
    note = "ticket 현재형 참조 #1234 (현행 foo 미언급)"
    out = upsert_reverify_frontmatter("---\nname: m\n---\n\nb\n", "stale", note, "2026-05-31")
    d = yaml.safe_load(_FM_RE.match(out).group(1))
    assert d["reverify_note"] == note                   # '#' 이후 truncate 안 됨


def test_write_back_idempotent_with_quoted_note(tmp_path):
    """JSON 인용 note 로도 idempotency 유지 (날짜만 달라지면 재기록 안 함)."""
    from reverify import write_back_verdict, StaleVerdict
    p = tmp_path / "m.md"
    p.write_text("---\nname: m\n---\n\nBGE-M3\n", encoding="utf-8")
    v = StaleVerdict(status="stale", note="embedding_model 현재형 참조 bge-m3 (현행 arctic 미언급)")
    assert write_back_verdict(p, v, "2026-05-31") is True
    first = p.read_text(encoding="utf-8")
    assert write_back_verdict(p, v, "2026-06-09") is False   # status/note 불변 → no-write
    assert p.read_text(encoding="utf-8") == first


def test_crlf_body_preserved_on_stale_writeback(tmp_path):
    """audit: CRLF 메모리가 stale flag 돼도 본문 line ending 이 LF 로 무단 변환 안 됨."""
    from reverify import write_back_verdict, StaleVerdict
    p = tmp_path / "m.md"
    p.write_bytes(b"---\r\nname: m\r\n---\r\n\r\nbody line1 bge-m3\r\nbody line2\r\n")
    assert write_back_verdict(p, StaleVerdict(status="stale", note="n"), "2026-05-31") is True
    raw = p.read_bytes()
    assert b"body line1 bge-m3\r\n" in raw      # 본문 CRLF 보존
    assert b"body line2\r\n" in raw
    assert b"reverify_status: stale" in raw     # flag 는 기록됨


def test_default_root_flat_deploy(tmp_path, monkeypatch):
    """audit: flat 배포(reverify.py 와 memory_indexer.py 같은 dir)에서 default_root 가
    그 dir 로 해석되고 verify_registry 통과(verifier 무력화 안 됨)."""
    import importlib.util
    import shutil
    import sys
    from pathlib import Path
    monkeypatch.delenv("MV3_REVERIFY_ROOT", raising=False)
    flat = tmp_path / "mindvault"
    flat.mkdir()
    src = Path(__file__).resolve().parent.parent / "src"
    shutil.copy2(src / "reverify.py", flat / "reverify.py")
    shutil.copy2(src / "memory_indexer.py", flat / "memory_indexer.py")  # grep 대상(arctic/8081)
    spec = importlib.util.spec_from_file_location("reverify_flat_test", flat / "reverify.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["reverify_flat_test"] = mod   # dataclass __module__ 해소(default_factory)
    try:
        spec.loader.exec_module(mod)
        assert mod.default_root() == flat                   # parent.parent overshoot 아님
        assert mod.verify_registry() == []                  # flat sibling 을 basename 폴백으로 찾음
        v = mod.check_memory_staleness("BGE-M3 포트 8765 사용")   # default_root 사용
        assert v.status == "stale"                           # 진짜 stale 을 fresh 로 흘리지 않음
    finally:
        sys.modules.pop("reverify_flat_test", None)


# ---- sweep round-2 fixes ----
def test_lone_cr_frontmatter_can_be_flagged(tmp_path):
    """audit R2: classic-Mac lone-CR(\\r-only) frontmatter 도 정규화돼 stale flag 가능
    (R1 _read_raw 가 lone-CR 을 그대로 둬 _FM_RE 미매칭→미flag 하던 회귀 차단)."""
    from reverify import write_back_verdict, StaleVerdict, _current_reverify_status, _read_raw
    p = tmp_path / "m.md"
    p.write_bytes(b"---\rname: m\r---\r\rbody bge-m3\r")   # lone CR (no \n)
    assert write_back_verdict(p, StaleVerdict(status="stale", note="n"), "2026-05-31") is True
    assert _current_reverify_status(_read_raw(p)) == "stale"


def test_read_raw_preserves_crlf_normalizes_lone_cr(tmp_path):
    """\\r\\n 은 보존, 단독 \\r 만 LF 로 정규화."""
    from reverify import _read_raw
    p = tmp_path / "m.md"
    p.write_bytes(b"a\r\nb\rc\n")
    assert _read_raw(p) == "a\r\nb\nc\n"   # CRLF 유지, lone CR → LF


def test_atomic_write_pid_unique_tmp(tmp_path, monkeypatch):
    """audit R2: _atomic_write tmp 가 PID-unique (동시 SessionEnd write race 차단)."""
    import os as _os
    import reverify
    captured = {}
    real = _os.replace

    def spy(src, dst):
        captured["src"] = str(src)
        return real(src, dst)

    monkeypatch.setattr(reverify.os, "replace", spy)
    p = tmp_path / "x.md"
    assert reverify._atomic_write(p, "data") is True
    assert f".{_os.getpid()}.tmp" in captured["src"]   # PID-scoped tmp
    assert p.read_text() == "data"
