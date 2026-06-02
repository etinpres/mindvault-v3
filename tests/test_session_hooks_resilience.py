"""bug-audit 2026-05-29 회귀 테스트 — SessionStart/End 훅 강건성.

커버하는 수정:
- session-hooks-subagent-fire-1: 서브에이전트 SessionStart(agent_type 포함)는 동기
  요약 생성 없이 즉시 종료.
- session-hooks-frontmatter-1: write_staged 가 LLM 값의 줄바꿈을 단일 라인으로
  정규화해 frontmatter 구조(특히 값 안의 '---')가 깨지지 않는다.
"""
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


class TestSubagentGate(unittest.TestCase):
    def test_subagent_start_skips_summary(self):
        import session_memory as sm
        payload = json.dumps({"session_id": "abc12345", "agent_type": "Explore"})
        with patch.object(sm, "trigger_arctic_warmup"), \
             patch.object(sm, "get_recent_sessions") as grs, \
             patch.object(sm, "call_gemma") as cg, \
             patch.object(sm.sys, "stdin", io.StringIO(payload)):
            rc = sm.main()
        self.assertEqual(rc, 0)
        cg.assert_not_called()
        grs.assert_not_called()

    def test_main_session_not_gated(self):
        """agent_type 없는 일반 SessionStart 는 게이트를 통과해 get_recent_sessions 도달."""
        import session_memory as sm
        payload = json.dumps({"session_id": "abc12345"})
        with patch.object(sm, "trigger_arctic_warmup"), \
             patch.object(sm, "get_recent_sessions", return_value=[]) as grs, \
             patch.object(sm, "call_gemma") as cg, \
             patch.object(sm.sys, "stdin", io.StringIO(payload)):
            rc = sm.main()
        self.assertEqual(rc, 0)
        grs.assert_called_once()  # 게이트 안 됨 — 정상 경로 진입
        cg.assert_not_called()    # get_recent_sessions 가 [] 라 요약 전 조기 return


class TestWriteStagedFrontmatterSanitize(unittest.TestCase):
    def test_newlines_in_values_do_not_break_frontmatter(self):
        import session_memory_end as sme
        from memory_indexer import parse_frontmatter
        with tempfile.TemporaryDirectory() as tmp:
            staged = Path(tmp) / "_staged"
            with patch.object(sme, "STAGED_DIR", staged), \
                 patch.object(sme, "staged_dir_for", lambda t: staged):
                item = {
                    "title": "제목\n둘째줄 악의적",
                    "type": "feedback",
                    "reason": "이유\n---\n가짜 종료",   # 값 안의 '---' 가 frontmatter 조기 종료 유발했었음
                    "evidence": "근거\nline2",
                    "body": "본문\n여러\n줄 보존",
                }
                p = sme.write_staged(item, "sess1234")
            self.assertIsNotNone(p)
            text = p.read_text(encoding="utf-8")
            meta, body = parse_frontmatter(text)
            # 모든 키 존재 = frontmatter 가 '---' 로 조기 종료되지 않음
            for key in ("name", "description", "type", "reason", "evidence"):
                self.assertIn(key, meta, f"{key} 누락 — frontmatter 구조 손상")
            self.assertEqual(meta["type"], "feedback")
            # 값에 줄바꿈 없음
            self.assertNotIn("\n", str(meta["name"]))
            self.assertNotIn("\n", str(meta["reason"]))
            # 본문 보존
            self.assertIn("본문", body)
            self.assertIn("줄 보존", body)


if __name__ == "__main__":
    unittest.main()
