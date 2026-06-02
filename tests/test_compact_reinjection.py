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


def test_handle_compact_short_query_skips(tmp_path, monkeypatch, capsys):
    """길이 게이트(COMPACT_MIN_QUERY_LEN)만으로 스킵됨을 핀 — intent 게이트를 False 로
    중화하고 recall spy 로 '길이 때문에 recall 미호출' 을 관측 (mutation: 길이게이트
    제거 시 recall 호출돼 실패)."""
    import session_memory as sm
    import memory_search
    import query_intent
    t = tmp_path / "s.jsonl"
    # len 7 < 8, 그리고 noise 아님 — 길이 게이트만 걸려야 함
    _write_raw(t, [{"type": "user", "message": {"content": "fix bug"}}])
    monkeypatch.setattr(query_intent, "should_skip_recall", lambda obj: False)  # intent 중화
    ran = {}
    monkeypatch.setattr(memory_search, "recall_memory",
                        lambda *a, **k: (ran.update(hit=True), [])[1])
    rc = sm.handle_compact_reinjection({"transcript_path": str(t)})
    assert rc == 0
    assert ran.get("hit") is None  # 길이 게이트가 recall 전에 차단
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
    monkeypatch.setattr(sm, "trigger_arctic_warmup", lambda: None)
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
    monkeypatch.setattr(sm, "trigger_arctic_warmup", lambda: None)
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
    """tail-keep + per-turn cap 둘 다 핀 — truncation 을 실제로 강제하는 fixture.
    (head-keep 으로 바꾸거나 per-turn cap 을 제거하면 각각 다른 assert 가 실패해야 함.)"""
    import re as _re
    import session_memory as sm
    t = tmp_path / "s.jsonl"
    # 4개 턴 모두 길게(>MAX_MSG_CHARS) + 최신 턴 시작에 키워드, 본문은 'Z' 5000개
    _write_raw(t, [
        {"type": "user", "message": {"content": "OLDA " + "A" * 5000}},
        {"type": "user", "message": {"content": "OLDB " + "B" * 5000}},
        {"type": "user", "message": {"content": "OLDC " + "C" * 5000}},
        {"type": "user", "message": {"content": "NEWESTKW " + "Z" * 5000}},
    ])
    q = sm.extract_compact_query(t)
    # (a) tail-keep: 최신 턴 시작 키워드 생존 (head-keep 이면 1200 안에 안 들어와 실패)
    assert "NEWESTKW" in q
    # (b) per-turn cap: 최신 턴 본문이 MAX_MSG_CHARS 로 잘림 → 최장 연속 Z ≤ cap
    #     (cap 제거 시 tail 1200 이 전부 Z 라 최장 Z=1200 > 400 → 실패, 동시에 키워드도 사라져 (a) 도 실패)
    longest_z = max((len(m.group(0)) for m in _re.finditer(r"Z+", q)), default=0)
    assert longest_z <= sm.MAX_MSG_CHARS
    # (c) tail-keep: 가장 오래된 턴 키워드는 잘려나감
    assert "OLDA" not in q
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
    monkeypatch.setattr(sm, "trigger_arctic_warmup", lambda: None)
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
    """intent skip 시 recall_memory 가 *호출 안 됨* 을 spy 플래그로 검증.
    sentinel 을 raise 하면 handle 의 broad except 가 삼켜 vacuous-pass 가 되므로
    (round-2 audit), 예외 대신 mutable flag 로 관측한다."""
    import session_memory as sm
    import memory_search
    import query_intent
    t = tmp_path / "s.jsonl"
    _write_raw(t, [{"type": "user", "message": {"content": "충분히 긴 어떤 질의 텍스트입니다"}}])
    monkeypatch.setattr(query_intent, "should_skip_recall", lambda obj: True)
    ran = {}

    def _spy(*a, **k):
        ran["hit"] = True
        return []

    monkeypatch.setattr(memory_search, "recall_memory", _spy)
    rc = sm.handle_compact_reinjection({"transcript_path": str(t)})
    assert rc == 0
    assert ran.get("hit") is None, "intent-skip 제거 시 recall 이 불려 이 assert 가 실패해야 함(mutation 감지)"
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


# --- round-2 fix: intent 는 join-blob 이 아니라 최신 genuine 턴만 classify --------

def test_compact_intent_skips_only_when_all_turns_chat(tmp_path, monkeypatch):
    """최근 genuine 턴이 *전부* chat/meta 일 때만 스킵. 일부라도 substantive 면 회수 진행.
    (latest-only 규칙이면 final ack 가 substantive 맥락을 억제 → 이 테스트가 실패.
     any 규칙이면 oldest 인사말이 전체 억제 → 케이스 A 가 실패.)"""
    import session_memory as sm
    import memory_search
    import query_intent
    monkeypatch.setattr(query_intent, "classify", lambda txt: txt)  # passthrough
    monkeypatch.setattr(query_intent, "should_skip_recall", lambda txt: "CHATMARK" in txt)
    ran = {}
    monkeypatch.setattr(memory_search, "recall_memory",
                        lambda *a, **k: (ran.update(hit=True), [])[1])

    # (A) 일부 substantive (oldest substantive, latest 는 chat-ack) → 회수 진행
    t = tmp_path / "mixed.jsonl"
    _write_raw(t, [
        {"type": "user", "message": {"content": "substantive 실제 분석 요청 텍스트입니다"}},
        {"type": "user", "message": {"content": "CHATMARK ok 고마워"}},
    ])
    ran.clear()
    sm.handle_compact_reinjection({"transcript_path": str(t)})
    assert ran.get("hit") is True  # 일부 substantive → 회수 (latest-only/any 규칙이면 skip → 실패)

    # (B) 전부 chat → skip
    t2 = tmp_path / "allchat.jsonl"
    _write_raw(t2, [
        {"type": "user", "message": {"content": "CHATMARK 안녕 충분히 긴 인사말"}},
        {"type": "user", "message": {"content": "CHATMARK ok 고마워 잘했어"}},
    ])
    ran.clear()
    sm.handle_compact_reinjection({"transcript_path": str(t2)})
    assert ran.get("hit") is None  # 전부 chat → skip (recall 미호출)


# --- round-4 fix: SIGALRM 핸들러 누수 방지 (장수 인터프리터 flaky 차단) ----------

def test_compact_reinjection_restores_sigalrm_handler(tmp_path, monkeypatch):
    """호출 후 이전 SIGALRM 핸들러로 복원돼야 함. 안 하면 _compact_alarm 이 남아
    이후 stray alarm 이 _CompactTimeout(BaseException)을 던져 무관 코드/테스트가 flaky."""
    import signal as _sig
    import session_memory as sm
    import memory_search
    t = tmp_path / "s.jsonl"
    _write_raw(t, [{"type": "user", "message": {"content": "충분히 긴 회수 질의 텍스트 압축 분석"}}])
    monkeypatch.setattr(memory_search, "recall_memory",
                        lambda *a, **k: [{"name": "x", "source": ["vec"], "description": "d", "snippet": "", "score": 0.9}])
    # SIG_DFL 이 아닌 *구별 가능한* sentinel 을 prior 핸들러로 설치 → "항상 SIG_DFL 로
    # 복원" mutant 도 잡는다 (restore-to-PRIOR 를 검증).
    sentinel = lambda s, f: None
    prev = _sig.signal(_sig.SIGALRM, sentinel)
    try:
        sm.handle_compact_reinjection({"transcript_path": str(t)})
        after = _sig.getsignal(_sig.SIGALRM)
        assert after is sentinel              # 이전 핸들러로 복원 (누수/SIG_DFL-always 면 실패)
        assert after is not sm._compact_alarm
    finally:
        _sig.signal(_sig.SIGALRM, prev)


def test_compact_metric_outcomes_for_skips(tmp_path, monkeypatch):
    """skip 경로 outcome 도 metrics 에 기록 (injected 외 종결점 가드)."""
    import json as _json
    import session_memory as sm
    mlog = tmp_path / "metrics.jsonl"
    monkeypatch.setattr(sm, "_METRICS_LOG", mlog)
    sm.handle_compact_reinjection({})  # transcript 미해결 → no_transcript
    t = tmp_path / "short.jsonl"
    _write_raw(t, [{"type": "user", "message": {"content": "hi"}}])  # len 2 < 8 → short_query
    sm.handle_compact_reinjection({"transcript_path": str(t)})
    recs = [_json.loads(l) for l in mlog.read_text().splitlines() if l.strip()]
    outcomes = {r.get("outcome") for r in recs if r.get("kind") == "compact_reinject"}
    assert "no_transcript" in outcomes
    assert "short_query" in outcomes


def test_compact_reinjection_records_metric(tmp_path, monkeypatch):
    """compact 종결점이 metrics.jsonl 에 1줄 기록 (Layer 4 대칭)."""
    import json as _json
    import session_memory as sm
    import memory_search
    mlog = tmp_path / "metrics.jsonl"
    monkeypatch.setattr(sm, "_METRICS_LOG", mlog)
    t = tmp_path / "s.jsonl"
    _write_raw(t, [{"type": "user", "message": {"content": "MindVault 회수 게이트 구조 분석 요청"}}])
    monkeypatch.setattr(memory_search, "recall_memory",
                        lambda *a, **k: [{"name": "x", "source": ["vec"], "description": "d", "snippet": "", "score": 0.9}])
    sm.handle_compact_reinjection({"transcript_path": str(t)})
    assert mlog.exists()
    recs = [_json.loads(l) for l in mlog.read_text().splitlines() if l.strip()]
    assert any(r.get("kind") == "compact_reinject" and r.get("outcome") == "injected" and r.get("picked") == 1
               for r in recs)
