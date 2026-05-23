"""Sprint 13 — Procedural Memory Slot 통합 테스트.

검증 대상:
- memory_extractor: type=procedural 항목 valid 통과 + trigger 패턴
- session_memory_end: staged_dir_for("procedural") → PROCEDURAL_STAGED_DIR
- memory_review_cli: promote target 분기 + 양쪽 staged 스캔
"""
from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

# 다른 테스트가 production 위치의 memory_extractor 를 먼저 import 해 캐시한
# 경우 worktree 신규 함수가 안 보이므로 모듈 재로드를 강제한다.
for _mod in ("memory_extractor", "session_memory_end", "memory_review_cli"):
    sys.modules.pop(_mod, None)


class TestExtractorProceduralType(unittest.TestCase):
    def test_valid_types_includes_procedural(self):
        from memory_extractor import VALID_TYPES
        self.assertIn("procedural", VALID_TYPES)

    def test_trigger_matches_procedural_keywords(self):
        from memory_extractor import has_trigger
        cases = [
            "이 명령어 외워둬: claude --bg",
            "이 syntax 자주 쓰니까 기억해둬",
            "이렇게 하면 백그라운드 실행돼",
            "이 workflow 반복해서 쓸 거야",
            "환경설정 한 줄: export MV2_EXTRA_MEMORY_DIRS=...",
            "이 옵션 외워둬",
            "이 flag 자주 쓴다",
        ]
        for txt in cases:
            self.assertTrue(
                has_trigger([{"role": "user", "text": txt}]),
                f"trigger 누락: {txt!r}",
            )

    def test_trigger_does_not_match_chitchat(self):
        from memory_extractor import has_trigger
        cases = [
            "안녕하세요 오늘 날씨 어떄요",
            "그냥 이거 한번 돌려봐",
            "테스트 결과 어떻게 됐어?",
        ]
        for txt in cases:
            self.assertFalse(
                has_trigger([{"role": "user", "text": txt}]),
                f"잘못된 trigger: {txt!r}",
            )

    def test_parse_gemma_json_accepts_procedural(self):
        from memory_extractor import parse_gemma_json
        out = json.dumps(
            [
                {
                    "type": "procedural",
                    "title": "claude --bg syntax",
                    "body": "claude --bg \"prompt\" # 백그라운드 세션 시작",
                    "reason": "자주 사용",
                    "evidence": "이 명령어 외워둬",
                }
            ]
        )
        items = parse_gemma_json(out)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["type"], "procedural")

    def test_parse_gemma_json_rejects_bogus_type(self):
        from memory_extractor import parse_gemma_json
        out = json.dumps(
            [{"type": "bogus", "title": "x", "body": "y"}]
        )
        self.assertEqual(parse_gemma_json(out), [])


class TestAutoTriggerHeuristic(unittest.TestCase):
    """Sprint NEXT-1 — assistant Bash tool_use + user 다음 액션 자동 trigger."""

    def test_special_bash_then_next_action_triggers(self):
        from memory_extractor import has_trigger
        msgs = [
            {
                "role": "assistant",
                "text": "",
                "bash_commands": [
                    "launchctl kickstart -k gui/501/com.yonghaekim.gemma-mlx"
                ],
            },
            {"role": "user", "text": "영구화 필요", "bash_commands": []},
        ]
        self.assertTrue(has_trigger(msgs))

    def test_non_trivial_bash_then_next_action_triggers(self):
        from memory_extractor import has_trigger
        long_cmd = (
            "curl -s https://example.com/api/v1/long/endpoint?param=1&another=2 "
            '| jq ".data" > /tmp/out.json && echo done'
        )
        msgs = [
            {"role": "assistant", "text": "", "bash_commands": [long_cmd]},
            {"role": "user", "text": "적용해줘", "bash_commands": []},
        ]
        self.assertTrue(has_trigger(msgs))

    def test_trivial_bash_does_not_trigger(self):
        from memory_extractor import has_trigger
        msgs = [
            {"role": "assistant", "text": "", "bash_commands": ["ls -la"]},
            {"role": "user", "text": "진행", "bash_commands": []},
        ]
        self.assertFalse(has_trigger(msgs))

    def test_special_bash_without_next_action_does_not_trigger(self):
        from memory_extractor import has_trigger
        msgs = [
            {
                "role": "assistant",
                "text": "",
                "bash_commands": ['sqlite3 db.db "SELECT 1"'],
            },
            {"role": "user", "text": "그래서 결과는?", "bash_commands": []},
        ]
        self.assertFalse(has_trigger(msgs))

    def test_long_next_action_message_does_not_trigger(self):
        """잡담·서술이 길게 섞인 메시지는 next_action 매칭돼도 trigger 안 켬."""
        from memory_extractor import has_trigger
        msgs = [
            {
                "role": "assistant",
                "text": "",
                "bash_commands": ["launchctl load -w ~/Library/LaunchAgents/x.plist"],
            },
            {
                "role": "user",
                "text": (
                    "아 이거 적용 안 되네 다른 방법으로 시도해야겠다 한 번 더 "
                    "원인 분석부터 다시 해보자 진행 상황 정리도 필요해"
                ),
                "bash_commands": [],
            },
        ]
        self.assertFalse(has_trigger(msgs))

    def test_signal_resets_after_user_turn(self):
        from memory_extractor import has_trigger
        msgs = [
            {
                "role": "assistant",
                "text": "",
                "bash_commands": ["launchctl load -w x.plist"],
            },
            {"role": "user", "text": "결과 보여줘", "bash_commands": []},
            {"role": "assistant", "text": "결과는 ...", "bash_commands": []},
            {"role": "user", "text": "진행", "bash_commands": []},
        ]
        self.assertFalse(has_trigger(msgs))

    def test_signal_accumulates_across_split_assistant(self):
        """assistant 가 tool_use → text 로 분할된 케이스도 신호 유지."""
        from memory_extractor import has_trigger
        msgs = [
            {"role": "user", "text": "환경 설정 부탁", "bash_commands": []},
            {
                "role": "assistant",
                "text": "",
                "bash_commands": ["launchctl load -w x.plist"],
            },
            {"role": "assistant", "text": "셋업 완료", "bash_commands": []},
            {"role": "user", "text": "영구화", "bash_commands": []},
        ]
        self.assertTrue(has_trigger(msgs))

    def test_text_trigger_still_works_with_new_message_shape(self):
        from memory_extractor import has_trigger
        msgs = [{"role": "user", "text": "이 명령어 외워둬", "bash_commands": []}]
        self.assertTrue(has_trigger(msgs))


class TestExtractBashFromContent(unittest.TestCase):
    def test_extracts_bash_command(self):
        from memory_extractor import extract_bash_from_content
        content = [
            {"type": "text", "text": "준비"},
            {
                "type": "tool_use",
                "name": "Bash",
                "input": {"command": "sqlite3 a.db", "description": "x"},
            },
        ]
        self.assertEqual(extract_bash_from_content(content), ["sqlite3 a.db"])

    def test_ignores_non_bash_tools(self):
        from memory_extractor import extract_bash_from_content
        content = [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/a"}},
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "/b"}},
        ]
        self.assertEqual(extract_bash_from_content(content), [])

    def test_redacts_secrets(self):
        from memory_extractor import extract_bash_from_content
        content = [
            {
                "type": "tool_use",
                "name": "Bash",
                "input": {
                    "command": "curl -H 'Authorization: Bearer abc123def456ghi789jkl012'"
                },
            }
        ]
        out = extract_bash_from_content(content)
        self.assertEqual(len(out), 1)
        self.assertIn("[REDACTED]", out[0])


class TestBuildPromptIncludesBash(unittest.TestCase):
    def test_bash_lines_appended(self):
        from memory_extractor import build_prompt
        msgs = [
            {"role": "user", "text": "환경 설정", "bash_commands": []},
            {
                "role": "assistant",
                "text": "",
                "bash_commands": ["launchctl load -w x.plist"],
            },
            {"role": "user", "text": "영구화", "bash_commands": []},
        ]
        p = build_prompt(msgs)
        self.assertIn("A:bash: launchctl", p)
        self.assertIn("U: 환경 설정", p)


class TestSessionEndStagedSlot(unittest.TestCase):
    def test_staged_dir_for_procedural(self):
        import session_memory_end as sme
        self.assertEqual(sme.staged_dir_for("procedural"), sme.PROCEDURAL_STAGED_DIR)

    def test_staged_dir_for_feedback(self):
        import session_memory_end as sme
        self.assertEqual(sme.staged_dir_for("feedback"), sme.STAGED_DIR)

    def test_staged_dir_for_project(self):
        import session_memory_end as sme
        self.assertEqual(sme.staged_dir_for("project"), sme.STAGED_DIR)

    def test_write_staged_routes_by_type(self):
        """write_staged 가 type 별로 올바른 디렉토리에 파일 작성."""
        import session_memory_end as sme
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            with patch.object(sme, "MEMORY_DIR", tmp_path), \
                 patch.object(sme, "STAGED_DIR", tmp_path / "_staged"), \
                 patch.object(sme, "PROCEDURAL_DIR", tmp_path / "_procedural"), \
                 patch.object(
                     sme,
                     "PROCEDURAL_STAGED_DIR",
                     tmp_path / "_procedural" / "_staged",
                 ):
                item_p = {
                    "type": "procedural",
                    "title": "claude bg",
                    "body": "claude --bg",
                    "reason": "외워둬",
                    "evidence": "이 명령어",
                }
                item_f = {
                    "type": "feedback",
                    "title": "no force push",
                    "body": "절대 강제 푸시 금지",
                    "reason": "원칙",
                    "evidence": "다음부턴",
                }
                p1 = sme.write_staged(item_p, "abc12345")
                p2 = sme.write_staged(item_f, "abc12345")
                self.assertIsNotNone(p1)
                self.assertIsNotNone(p2)
                self.assertIn("_procedural/_staged", str(p1))
                self.assertNotIn("_procedural", str(p2))


class TestReviewCliRoutes(unittest.TestCase):
    def test_promote_target_dir(self):
        import memory_review_cli as mrc
        self.assertEqual(mrc._promote_target_dir("procedural"), mrc.PROCEDURAL_DIR)
        self.assertEqual(mrc._promote_target_dir("feedback"), mrc.MEMORY_DIR)
        self.assertEqual(mrc._promote_target_dir("project"), mrc.MEMORY_DIR)

    def test_safe_staged_path_finds_in_either_slot(self):
        import memory_review_cli as mrc
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staged = root / "_staged"
            proc_staged = root / "_procedural" / "_staged"
            staged.mkdir(parents=True)
            proc_staged.mkdir(parents=True)
            (staged / "a.md").write_text("a")
            (proc_staged / "b.md").write_text("b")
            with patch.object(mrc, "STAGED_DIR", staged), \
                 patch.object(mrc, "PROCEDURAL_STAGED_DIR", proc_staged), \
                 patch.object(mrc, "STAGED_DIRS", (staged, proc_staged)):
                self.assertEqual(mrc._safe_staged_path("a.md"), staged / "a.md")
                self.assertEqual(mrc._safe_staged_path("b.md"), proc_staged / "b.md")
                self.assertIsNone(mrc._safe_staged_path("../evil.md"))
                self.assertIsNone(mrc._safe_staged_path("c.md"))

    def test_cmd_list_merges_both_slots(self):
        """cmd_list 가 _staged + _procedural/_staged 양쪽 스캔."""
        import memory_review_cli as mrc
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staged = root / "_staged"
            proc_staged = root / "_procedural" / "_staged"
            staged.mkdir(parents=True)
            proc_staged.mkdir(parents=True)
            (staged / "20260101-000000_feedback_a.md").write_text(
                "---\nname: a\ntype: feedback\n---\nbody a"
            )
            (proc_staged / "20260101-000001_procedural_b.md").write_text(
                "---\nname: b\ntype: procedural\n---\nbody b"
            )
            import io
            buf = io.StringIO()
            with patch.object(mrc, "STAGED_DIR", staged), \
                 patch.object(mrc, "PROCEDURAL_STAGED_DIR", proc_staged), \
                 patch.object(mrc, "STAGED_DIRS", (staged, proc_staged)), \
                 patch("sys.stdout", buf):
                mrc.cmd_list()
            out = json.loads(buf.getvalue())
            types = sorted([it["type"] for it in out["staged"]])
            self.assertEqual(types, ["feedback", "procedural"])


if __name__ == "__main__":
    unittest.main()
