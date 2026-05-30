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


# === round-1 audit fixes 회귀 가드 ==========================================
import pytest


def _write_raw(path, entries):
    """임의 JSONL 엔트리(최상위 isCompactSummary 등) 작성용."""
    with path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")


# --- F2: query 오염 차단 (isCompactSummary / skill body / local-command) -----

def test_extract_query_skips_iscompactsummary(tmp_path):
    import session_memory as sm
    t = tmp_path / "s.jsonl"
    _write_raw(t, [
        {"type": "user", "message": {"content": "진짜 사용자 질문 처음"}},
        {"type": "user", "isCompactSummary": True,
         "message": {"content": "This session is being continued. Summary: " + "x" * 5000}},
        {"type": "user", "message": {"content": "압축 후 진짜 사용자 질문입니다"}},
    ])
    q = sm.extract_compact_query(t)
    assert "압축 후 진짜 사용자 질문입니다" in q
    assert "This session is being continued" not in q
    assert "Summary:" not in q


def test_extract_query_skips_local_command_and_skill_blocks(tmp_path):
    import session_memory as sm
    t = tmp_path / "s.jsonl"
    _write_raw(t, [
        {"type": "user", "message": {"content": "<local-command-caveat>\nCaveat: ...\n</local-command-caveat>"}},
        {"type": "user", "message": {"content": "<local-command-stdout>설정됨</local-command-stdout>"}},
        {"type": "user", "message": {"content": "Base directory for this skill: /x/y\n# Writing Plans\n" + "p" * 6000}},
        {"type": "user", "message": {"content": "이게 유일한 진짜 사용자 발화"}},
    ])
    q = sm.extract_compact_query(t)
    assert q.strip() == "이게 유일한 진짜 사용자 발화"


def test_realistic_post_compaction_transcript_yields_real_prompt(tmp_path):
    """F3: 실제 post-/compact transcript 형태에서 query 가 진짜 prompt 여야 한다."""
    import session_memory as sm
    t = tmp_path / "s.jsonl"
    _write_raw(t, [
        {"type": "user", "message": {"content": "Base directory for this skill: /a/b\n# Writing Plans\n" + "s" * 6000}},
        {"type": "user", "isCompactSummary": True,
         "message": {"content": "This session is being continued...\nSummary:\n" + "z" * 12000}},
        {"type": "user", "message": {"content": "<local-command-caveat>\nCaveat: msg\n</local-command-caveat>"}},
        {"type": "user", "message": {"content": "<local-command-stdout>goal set</local-command-stdout>"}},
        {"type": "user", "message": {"content": "compact hook 버그를 찾아서 고쳐줘"}},
    ])
    q = sm.extract_compact_query(t)
    assert "compact hook 버그를 찾아서 고쳐줘" in q
    assert "Writing Plans" not in q
    assert "This session is being continued" not in q
    assert "local-command" not in q


# --- F4/F16: per-turn cap + tail-keep 으로 최신 발화 보존 ---------------------

def test_extract_query_preserves_newest_turn_under_large_old_turns(tmp_path):
    import session_memory as sm
    t = tmp_path / "s.jsonl"
    _write_raw(t, [
        {"type": "user", "message": {"content": "오래된거 " + "A" * 5000}},
        {"type": "user", "message": {"content": "두번째 " + "B" * 5000}},
        {"type": "user", "message": {"content": "최신 진짜 의도 키워드 ZZZZ"}},
    ])
    q = sm.extract_compact_query(t)
    assert "최신 진짜 의도 키워드 ZZZZ" in q  # 앞에서 잘렸으면 실패
    assert len(q) <= sm.COMPACT_QUERY_MAX_CHARS


# --- F8: deque(maxlen=N) 마지막 N genuine 만 보존 ----------------------------

def test_extract_query_keeps_last_n_genuine(tmp_path):
    import session_memory as sm
    t = tmp_path / "s.jsonl"
    _write_raw(t, [{"type": "user", "message": {"content": f"발화{i} 충분히김 텍스트"}} for i in range(20)])
    q = sm.extract_compact_query(t, recent_user_turns=3)
    assert "발화19" in q and "발화18" in q and "발화17" in q
    assert "발화16" not in q


# --- F11: stale transcript_path → sid 폴백 -----------------------------------

def test_resolve_transcript_stale_path_falls_back_to_sid(tmp_path, monkeypatch):
    import session_memory as sm
    monkeypatch.setattr(sm, "PROJECTS_DIR", tmp_path)
    sid_file = tmp_path / "thesid.jsonl"
    sid_file.write_text("{}\n")
    got = sm._resolve_transcript({
        "transcript_path": str(tmp_path / "stale_missing.jsonl"),
        "session_id": "thesid",
    })
    assert got == sid_file


# --- F12: main() source 정규화 (대소문자/공백/없음) --------------------------

@pytest.mark.parametrize("src", ["compact", "COMPACT", " compact ", "Compact"])
def test_main_source_variants_route_compact(monkeypatch, src):
    import session_memory as sm
    monkeypatch.setattr(sm, "trigger_bge_m3_warmup", lambda: None)
    monkeypatch.delenv("MV3_HOOK_RECURSION_GUARD", raising=False)
    called = {}
    monkeypatch.setattr(sm, "handle_compact_reinjection",
                        lambda hd: (called.update(hit=True), 0)[1])
    monkeypatch.setattr(sm, "call_gemma",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("summary on compact")))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s", "source": src})))
    assert sm.main() == 0
    assert called.get("hit") is True


# --- F1: recall 시간예산 → _CompactTimeout silent skip -----------------------

def test_compact_recall_time_budget_skips_on_slow(tmp_path, monkeypatch, capsys):
    import time as _t
    import session_memory as sm
    import memory_search
    monkeypatch.setattr(sm, "COMPACT_BUDGET_S", 0.3)
    t = tmp_path / "s.jsonl"
    _write_raw(t, [{"type": "user", "message": {"content": "충분히 긴 진짜 회수 질의입니다 압축"}}])

    def _slow(*a, **k):
        _t.sleep(2.0)  # budget(0.3s) 초과 → SIGALRM 이 interrupt
        return [{"name": "x", "source": ["vec"], "description": "d", "snippet": "", "score": 0.9}]

    monkeypatch.setattr(memory_search, "recall_memory", _slow)
    rc = sm.handle_compact_reinjection({"transcript_path": str(t)})
    assert rc == 0
    assert capsys.readouterr().out == ""  # 시간초과 → 주입 없음


# --- F9: chat/meta 의도면 recall 전 스킵 -------------------------------------

def test_compact_honors_intent_skip(tmp_path, monkeypatch, capsys):
    import session_memory as sm
    import memory_search
    import query_intent
    t = tmp_path / "s.jsonl"
    _write_raw(t, [{"type": "user", "message": {"content": "충분히 긴 어떤 질의 텍스트입니다"}}])
    monkeypatch.setattr(query_intent, "should_skip_recall", lambda obj: True)
    monkeypatch.setattr(memory_search, "recall_memory",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("recall ran despite intent skip")))
    rc = sm.handle_compact_reinjection({"transcript_path": str(t)})
    assert rc == 0
    assert capsys.readouterr().out == ""


# --- F5: format_memory_context source 스칼라 가드 (recall_core) --------------

def test_format_memory_context_scalar_source_not_char_split():
    import recall_core
    out = recall_core.format_memory_context(
        [{"name": "m", "source": "vec", "description": "d", "snippet": "", "score": 0.6}],
        wrap_system_reminder=False,
    )
    assert "v+e+c" not in out  # 스칼라가 글자단위 분해되면 안 됨
    assert "vec" in out
