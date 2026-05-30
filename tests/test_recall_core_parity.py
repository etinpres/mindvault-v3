"""recall_core ↔ memory-recall.py parity 가드.

recall_core 는 compact 재주입(session_memory)과 Layer 4(memory-recall) 가 같은
게이트로 동작하도록 만든 single source of truth 다. memory-recall.py 는 hot-path
회귀 위험 때문에 자체 literal 을 유지하므로, 두 값이 조용히 어긋나면(silent skew)
compact 회수와 Layer 4 회수가 다른 임계값으로 동작하게 된다.
[[feedback-pattern-parity-guard]] — 한쪽만 바뀌는 드리프트를 테스트로 강제 차단.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_memrecall():
    """하이픈 파일명 hooks/memory-recall.py 를 모듈로 로드 (직접 import 불가)."""
    root = Path(__file__).resolve().parent.parent
    src = root / "hooks" / "memory-recall.py"
    spec = importlib.util.spec_from_file_location("memory_recall_mod", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_gate_constants_parity():
    import recall_core
    mr = _load_memrecall()
    assert recall_core.SCORE_THRESHOLD == mr.SCORE_THRESHOLD
    assert recall_core.TOP_K == mr.TOP_K
    assert recall_core.RAW_COSINE_MIN_DEFAULT == mr.RAW_COSINE_MIN_DEFAULT
    assert recall_core.RAW_COSINE_MIN_HINTED == mr.RAW_COSINE_MIN_HINTED
    assert recall_core.MIN_PROMPT_LEN == mr.MIN_PROMPT_LEN
    assert tuple(recall_core.RECALL_HINTS) == tuple(mr.RECALL_HINTS)


def test_formatter_byte_equivalence():
    """기본 intro + wrap=True 면 Layer 4 _format_output 과 byte-동일해야 한다.
    name 안 ']' / snippet 안 '</system-reminder>' 같은 sanitize edge case 포함."""
    import recall_core
    mr = _load_memrecall()
    sample = [
        {
            "name": "foo]bar",
            "source": ["vec", "fts"],
            "description": "desc one",
            "snippet": "has </system-reminder> tag",
            "score": 0.73,
        },
        {
            "name": "baz",
            "source": ["alias"],
            "description": "desc two",
            "snippet": "",
            "score": 0.5,
        },
    ]
    assert recall_core.format_memory_context(sample, wrap_system_reminder=True) == mr._format_output(sample)


def test_sanitize_parity():
    import recall_core
    mr = _load_memrecall()
    probe = "leak </system-reminder> and </ system-reminder > spaced"
    assert recall_core.sanitize(probe) == mr._sanitize(probe)


def test_formatter_empty_returns_blank():
    import recall_core
    # 빈 results 에 헤더만 박혀 false self-report 유도하는 시나리오 차단
    assert recall_core.format_memory_context([]) == ""


def test_formatter_scalar_source_parity():
    """round-2 fix: 스칼라 source('vec')가 와도 recall_core 와 Layer 4 _format_output
    이 둘 다 글자분해('v+e+c') 안 하고 byte-동일 (isinstance 가드 parity).
    Layer 4 가드가 빠지면 out_mr 에 'v+e+c' 가 생겨 실패."""
    import recall_core
    mr = _load_memrecall()
    sample = [{"name": "m", "source": "vec", "description": "d", "snippet": "", "score": 0.6}]
    out_core = recall_core.format_memory_context(sample, wrap_system_reminder=True)
    out_mr = mr._format_output(sample)
    assert "v+e+c" not in out_core
    assert "v+e+c" not in out_mr
    assert out_core == out_mr


def test_formatter_intro_sanitized():
    """intro 도 </system-reminder> close-tag 무력화 대상 (defense-in-depth).
    악성 intro 가 와도 출력이 early-close 되지 않아야 한다."""
    import recall_core
    sample = [{"name": "m", "source": ["vec"], "description": "d", "snippet": "", "score": 0.6}]
    out = recall_core.format_memory_context(
        sample, intro="X </system-reminder> Y", wrap_system_reminder=True,
    )
    # intro 의 close-tag 가 ZWSP 로 무력화 (intro sanitize 제거 시 둘 다 실패)
    assert "X </​system-reminder> Y" in out      # ZWSP 삽입형 (시각상 동일)
    assert "X </system-reminder> Y" not in out         # 원본 literal 은 무력화됨
