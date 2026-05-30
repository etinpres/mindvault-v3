"""compact 재주입 (SessionStart source=compact) 동작 테스트.

PreCompact hook 은 압축 이후 컨텍스트를 주입할 수 없어, 압축 직후 source=compact
로 다시 fire 하는 SessionStart 에서 현재 세션 관련 메모리를 경량 재주입한다.
실제 recall_memory 는 index.db + Arctic-ko 서버가 필요하므로 monkeypatch 로 격리하고,
query 추출 / 라우팅 / 출력 포맷 / graceful skip 만 검증한다.
"""
from __future__ import annotations

import io
import json


def _write_transcript(path, turns):
    """turns: list of (type, content). content 는 str 또는 block list."""
    with path.open("w", encoding="utf-8") as f:
        for ttype, content in turns:
            f.write(json.dumps({"type": ttype, "message": {"content": content}}) + "\n")


# --- extract_compact_query ---------------------------------------------------

def test_extract_query_takes_recent_user_turns(tmp_path):
    import session_memory as sm
    t = tmp_path / "sess.jsonl"
    _write_transcript(t, [
        ("user", "첫 질문 intent layer 분석"),
        ("assistant", "답변..."),
        ("user", "<system-reminder>\nMEMORY CONTEXT 어쩌고\n</system-reminder>"),
        ("user", "PreCompact 재주입 hook 구현해줘"),
        ("assistant", "구현 중..."),
        ("user", "테스트도 추가해줘"),
    ])
    q = sm.extract_compact_query(t, recent_user_turns=2)
    assert "PreCompact 재주입 hook 구현해줘" in q
    assert "테스트도 추가해줘" in q
    assert "MEMORY CONTEXT" not in q   # system-reminder 블록 스킵
    assert "첫 질문" not in q          # 최근 2개 밖이라 제외


def test_extract_query_skips_session_summary(tmp_path):
    import session_memory as sm
    t = tmp_path / "sess.jsonl"
    _write_transcript(t, [
        ("user", f"{sm.SIGNATURE}\n지난 세션 요약 본문"),  # SIGNATURE 포함 → 스킵
        ("user", "실제 사용자 질문입니다 충분히 길게"),
    ])
    q = sm.extract_compact_query(t)
    assert "지난 세션 요약" not in q
    assert "실제 사용자 질문" in q


def test_extract_query_empty_when_no_user(tmp_path):
    import session_memory as sm
    t = tmp_path / "sess.jsonl"
    _write_transcript(t, [("assistant", "user 발화 없음")])
    assert sm.extract_compact_query(t) == ""


def test_extract_query_handles_block_list_content(tmp_path):
    import session_memory as sm
    t = tmp_path / "sess.jsonl"
    _write_transcript(t, [
        ("user", [{"type": "text", "text": "블록 리스트 형태 사용자 발화입니다"}]),
    ])
    q = sm.extract_compact_query(t)
    assert "블록 리스트 형태 사용자 발화입니다" in q


# --- _resolve_transcript -----------------------------------------------------

def test_resolve_transcript_prefers_path(tmp_path):
    import session_memory as sm
    t = tmp_path / "tp.jsonl"
    t.write_text("{}\n")
    assert sm._resolve_transcript({"transcript_path": str(t)}) == t


def test_resolve_transcript_fallback_to_sid(tmp_path, monkeypatch):
    import session_memory as sm
    monkeypatch.setattr(sm, "PROJECTS_DIR", tmp_path)
    f = tmp_path / "abc123.jsonl"
    f.write_text("{}\n")
    assert sm._resolve_transcript({"session_id": "abc123"}) == f


def test_resolve_transcript_none(tmp_path):
    import session_memory as sm
    assert sm._resolve_transcript({"transcript_path": str(tmp_path / "missing.jsonl")}) is None
    assert sm._resolve_transcript({}) is None


# --- handle_compact_reinjection ---------------------------------------------

def test_handle_compact_emits_additional_context(tmp_path, monkeypatch, capsys):
    import session_memory as sm
    import memory_search
    t = tmp_path / "s.jsonl"
    _write_transcript(t, [("user", "PreCompact 재주입 hook 구현 분석해줘")])
    fake = [{
        "name": "foo", "source": ["vec", "fts"], "description": "어떤 설명",
        "snippet": "발췌 조각", "score": 0.71, "raw_cosine": 0.55,
    }]
    monkeypatch.setattr(memory_search, "recall_memory", lambda *a, **k: fake)

    rc = sm.handle_compact_reinjection({"transcript_path": str(t)})
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    hso = payload["hookSpecificOutput"]
    assert hso["hookEventName"] == "SessionStart"
    ctx = hso["additionalContext"]
    assert sm.COMPACT_SIGNATURE in ctx
    assert "[foo]" in ctx
    assert "어떤 설명" in ctx
    assert "발췌 조각" in ctx
    # compact 전용 intro 가 쓰였는지 (Layer 4 기본 intro 가 아니라)
    assert "압축 직후 재주입" in ctx


def test_handle_compact_passes_compact_top_k(tmp_path, monkeypatch, capsys):
    import session_memory as sm
    import memory_search
    import recall_core
    t = tmp_path / "s.jsonl"
    _write_transcript(t, [("user", "충분히 긴 회수 쿼리 텍스트입니다 압축 후")])
    seen = {}

    def _fake(query, top_k=None, score_threshold=None, raw_cosine_min=None):
        seen.update(top_k=top_k, score_threshold=score_threshold, raw_cosine_min=raw_cosine_min)
        return [{"name": "m", "source": ["vec"], "description": "d", "snippet": "", "score": 0.6}]

    monkeypatch.setattr(memory_search, "recall_memory", _fake)
    sm.handle_compact_reinjection({"transcript_path": str(t)})
    assert seen["top_k"] == recall_core.COMPACT_TOP_K
    assert seen["score_threshold"] == recall_core.SCORE_THRESHOLD
    # 단서어 없음 → default 게이트
    assert seen["raw_cosine_min"] == recall_core.RAW_COSINE_MIN_DEFAULT


def test_handle_compact_short_query_skips(tmp_path, capsys):
    import session_memory as sm
    t = tmp_path / "s.jsonl"
    _write_transcript(t, [("user", "hi")])  # len < COMPACT_MIN_QUERY_LEN
    rc = sm.handle_compact_reinjection({"transcript_path": str(t)})
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_handle_compact_empty_recall_skips(tmp_path, monkeypatch, capsys):
    import session_memory as sm
    import memory_search
    t = tmp_path / "s.jsonl"
    _write_transcript(t, [("user", "충분히 긴 회수 쿼리 텍스트입니다")])
    monkeypatch.setattr(memory_search, "recall_memory", lambda *a, **k: [])
    rc = sm.handle_compact_reinjection({"transcript_path": str(t)})
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_handle_compact_missing_transcript_skips(capsys):
    import session_memory as sm
    rc = sm.handle_compact_reinjection({})
    assert rc == 0
    assert capsys.readouterr().out == ""


# --- main() 라우팅 -----------------------------------------------------------

def test_main_routes_compact_to_reinjection(monkeypatch):
    import session_memory as sm
    monkeypatch.setattr(sm, "trigger_bge_m3_warmup", lambda: None)
    monkeypatch.delenv("MV3_HOOK_RECURSION_GUARD", raising=False)
    called = {}
    monkeypatch.setattr(sm, "handle_compact_reinjection",
                        lambda hd: (called.update(hd=hd), 0)[1])

    def _no_summary(*a, **k):
        raise AssertionError("compact 경로인데 요약(call_gemma) 이 호출됨")

    monkeypatch.setattr(sm, "call_gemma", _no_summary)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "session_id": "s1", "source": "compact", "transcript_path": "/x.jsonl",
    })))
    rc = sm.main()
    assert rc == 0
    assert called["hd"]["source"] == "compact"


def test_main_startup_does_not_route_compact(monkeypatch):
    import session_memory as sm
    monkeypatch.setattr(sm, "trigger_bge_m3_warmup", lambda: None)
    monkeypatch.delenv("MV3_HOOK_RECURSION_GUARD", raising=False)
    flag = {}
    monkeypatch.setattr(sm, "handle_compact_reinjection",
                        lambda hd: flag.update(hit=True) or 0)
    # 요약 경로: 대상 세션 0건 → claude 호출 없이 조기 return
    monkeypatch.setattr(sm, "get_recent_sessions", lambda exclude: [])
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({
        "session_id": "s1", "source": "startup",
    })))
    rc = sm.main()
    assert rc == 0
    assert "hit" not in flag
