"""Sprint 4 Task 5 — UserPromptSubmit hook 계약 검증."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

HOOK = Path(__file__).parent.parent / "hooks" / "memory-recall.py"


class TestHookIO(unittest.TestCase):
    """hook의 stdin/stdout 계약: 모든 실패 silent, exit 0."""

    def _run(self, payload: dict, timeout: float = 5.0) -> tuple[int, str, str]:
        r = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(payload).encode(),
            capture_output=True,
            timeout=timeout,
        )
        return r.returncode, r.stdout.decode(), r.stderr.decode()

    def test_short_prompt_empty_output(self):
        rc, out, _ = self._run({"prompt": "ㅇ"})
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_malformed_stdin_silent(self):
        r = subprocess.run(
            [sys.executable, str(HOOK)],
            input=b"not json at all",
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), b"")

    def test_no_prompt_field_silent(self):
        rc, out, _ = self._run({"session_id": "abc"})
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_empty_stdin_silent(self):
        r = subprocess.run(
            [sys.executable, str(HOOK)],
            input=b"",
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), b"")


class TestHookTimeoutPropagation(unittest.TestCase):
    """post-ship 회귀 — intent classify 의 broad except 가 _Timeout 을 swallow
    하면 HARD_TIMEOUT_MS 가 무력화돼 hook 이 budget 넘어서까지 동작.
    classify_with_gemma 안에서 _Timeout 이 떠도 outer 핸들러가 잡아야 함.
    """

    def _load_hook_module(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("hk", str(HOOK))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_timeout_in_gemma_reaches_outer_handler(self):
        hk = self._load_hook_module()
        import io
        from unittest.mock import patch

        # query_intent 가 import 된 시점에 patch 적용되도록 sys.path 보장
        for d in hk.SCRIPTS_DIRS:
            if d.is_dir() and str(d) not in sys.path:
                sys.path.insert(0, str(d))
        import query_intent

        debug_messages: list[str] = []
        with patch.object(
            query_intent,
            "classify_with_gemma",
            side_effect=hk._Timeout(),
        ), patch.dict(
            os.environ, {"MV3_GEMMA_INTENT": "1"}
        ), patch.object(
            hk, "_debug", side_effect=lambda m: debug_messages.append(m)
        ), patch.object(
            sys, "stdin", io.StringIO(json.dumps({"prompt": "어떤 짧은 질문입니까?"}))
        ):
            rc = hk.main()
        self.assertEqual(rc, 0)
        # _Timeout 이 outer 까지 도달했다면 "timeout ... skip" 로그가 남는다.
        # broad except 가 swallow 했다면 "intent classify skipped" 로 그치고
        # recall 까지 진행 → 다른 로그가 더 박힌다.
        joined = " | ".join(debug_messages)
        self.assertIn("timeout", joined.lower(), f"messages={debug_messages}")
        self.assertNotIn(
            "intent classify skipped", joined,
            f"_Timeout 이 swallow 됨: {debug_messages}",
        )


@unittest.skipIf(
    os.environ.get("MV3_SKIP_INTEGRATION") == "1",
    "MV3_SKIP_INTEGRATION=1",
)
class TestHookNormalFlow(unittest.TestCase):
    """실 BGE-M3 + ~/.claude/mindvault-v3/index.db 의존."""

    def test_real_query_format(self):
        r = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps({"prompt": "메일 보내는 도구"}).encode(),
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(r.returncode, 0)
        # threshold 통과한 결과가 있으면 system-reminder 포맷
        if r.stdout.strip():
            out = r.stdout.decode()
            self.assertIn("<system-reminder>", out)
            self.assertIn("메모리 회수 (Layer 4 hybrid)", out)
            self.assertIn("</system-reminder>", out)


if __name__ == "__main__":
    unittest.main()
