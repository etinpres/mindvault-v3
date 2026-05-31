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
    out = upsert_reverify_frontmatter("본문만 있음", "stale", "n1", "2026-05-31")
    assert out.startswith("---\n")
    assert "reverify_status: stale" in out
    assert "reverify_note: n1" in out
    assert "본문만 있음" in out


def test_upsert_preserves_body_and_existing_keys():
    text = "---\nname: m\ntype: feedback\n---\n\n본문 줄1\n본문 줄2\n"
    out = upsert_reverify_frontmatter(text, "stale", "note", "2026-05-31")
    assert "name: m" in out and "type: feedback" in out
    assert "본문 줄1" in out and "본문 줄2" in out
    assert "reverify_status: stale" in out


def test_upsert_replaces_existing_reverify_keys():
    text = "---\nname: m\nreverify_status: stale\nreverify_note: old\nreverify_checked: 2026-01-01\n---\n\nbody\n"
    out = upsert_reverify_frontmatter(text, "stale", "new", "2026-05-31")
    assert out.count("reverify_status:") == 1
    assert "reverify_note: new" in out
    assert "old" not in out


def test_upsert_note_oneline():
    out = upsert_reverify_frontmatter("body", "stale", "줄1\n줄2", "2026-05-31")
    assert "reverify_note: 줄1 줄2" in out
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
