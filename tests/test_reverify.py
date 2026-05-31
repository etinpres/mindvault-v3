"""Phase 1③ 신뢰성 검증 — stale 자동 감지 테스트."""
import pytest

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
