"""Sprint NEXT-10 — ACK 휴리스틱 trigger 테스트.

검증 대상:
- ACK_RE: 사용자의 한국어/영어 단순 confirmation 패턴 매칭 + 잡담 false positive 차단
- _is_significant_assistant: bash_commands OR text ≥ 200자
- has_trigger: significant assistant + 짧은 ACK 결합 시 True (NEXT-10 분기)
- MV3_EXTRACTOR_ACK_TRIGGER=0 환경 변수로 OFF 가능 (재로드 검증)
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

# 다른 테스트가 production 모듈 캐시했을 수 있으므로 강제 reload
for _mod in ("memory_extractor",):
    sys.modules.pop(_mod, None)


class TestAckRegex(unittest.TestCase):
    def test_ack_positives(self):
        from memory_extractor import ACK_RE
        positives = [
            "좋아", "좋아!", "좋네", "좋아요", "굳", "굿", "굿!",
            "good", "Good", "nice", "perfect", "훌륭",
            "ㅇㅇ", "ㅇㅋ", "어", "네", "예", "OK", "ok", "오케이", "콜",
            "땡큐", "thx", "thanks", "Thanks", "감사", "👍", "✓", "✔", "💯",
            "확인", "맞아", "맞네", "그래", "그러게", "그렇네", "완벽",
            "아주 좋", "좋아 ㅋㅋ", "good~", "OK!", "ㅇㅇㅋㅋ",
        ]
        for txt in positives:
            self.assertTrue(
                ACK_RE.search(txt.strip()),
                f"ACK_RE should match: {txt!r}",
            )

    def test_ack_negatives_substantive_replies(self):
        """긴 대답이나 명령 지시는 ACK 가 아니어야 함."""
        from memory_extractor import ACK_RE
        negatives = [
            "이렇게 해줘",
            "다음 단계로 진행",
            "왜 그런지 설명해줘",
            "OK 그럼 다음은 어떻게 가야 할까?",  # 길이는 짧지만 질문 — ACK 패턴 ^...$ 다 매칭 안 됨
            "안녕 사용자 오늘 뭐해",
            "음 잘 모르겠는데 한 번 더 봐줄래",
        ]
        for txt in negatives:
            self.assertFalse(
                ACK_RE.search(txt.strip()),
                f"ACK_RE should NOT match: {txt!r}",
            )


class TestSignificantAssistant(unittest.TestCase):
    def test_bash_commands_make_significant(self):
        from memory_extractor import _is_significant_assistant
        self.assertTrue(_is_significant_assistant({
            "role": "assistant",
            "text": "짧음",
            "bash_commands": ["ls"],
        }))

    def test_long_text_makes_significant(self):
        from memory_extractor import _is_significant_assistant
        long_text = "a" * 250
        self.assertTrue(_is_significant_assistant({
            "role": "assistant", "text": long_text, "bash_commands": [],
        }))

    def test_short_text_no_bash_is_insignificant(self):
        from memory_extractor import _is_significant_assistant
        self.assertFalse(_is_significant_assistant({
            "role": "assistant", "text": "응", "bash_commands": [],
        }))


class TestHasTriggerAckLayer(unittest.TestCase):
    def test_significant_assistant_plus_ack_triggers(self):
        from memory_extractor import has_trigger
        msgs = [
            {"role": "user", "text": "이 버그 fix 해줘"},
            {
                "role": "assistant",
                "text": "fix 적용했어",
                "bash_commands": ["git commit -m 'fix(x): y'"],
            },
            {"role": "user", "text": "좋아!"},
        ]
        self.assertTrue(has_trigger(msgs), "significant + ACK → trigger")

    def test_ack_without_significant_assistant_no_trigger(self):
        from memory_extractor import has_trigger
        msgs = [
            {"role": "user", "text": "안녕"},
            {"role": "assistant", "text": "안녕!", "bash_commands": []},
            {"role": "user", "text": "ㅇㅇ"},
        ]
        self.assertFalse(
            has_trigger(msgs),
            "insignificant assistant + ACK 면 trigger 안 됨 (잡담)",
        )

    def test_significant_without_ack_no_trigger(self):
        from memory_extractor import has_trigger
        long_text = "여기 긴 설명 " + "a" * 250
        msgs = [
            {"role": "user", "text": "설명해줘"},
            {"role": "assistant", "text": long_text, "bash_commands": []},
            {"role": "user", "text": "왜 그런 거야?"},  # ACK 아님
        ]
        self.assertFalse(has_trigger(msgs))

    def test_long_ack_text_no_trigger(self):
        """user turn 이 30자 초과면 ACK 게이트 통과 못 함 (긴 응답 = 추가 작업)."""
        from memory_extractor import has_trigger
        msgs = [
            {"role": "user", "text": "이거 해줘"},
            {
                "role": "assistant",
                "text": "했어",
                "bash_commands": ["git commit"],
            },
            {
                "role": "user",
                "text": "좋아 그럼 다음으로 X 도 처리하고 Y 도 같이 봐줘 부탁",
            },
        ]
        self.assertFalse(has_trigger(msgs))

    def test_env_disables_ack_trigger(self):
        with patch.dict(os.environ, {"MV3_EXTRACTOR_ACK_TRIGGER": "0"}):
            sys.modules.pop("memory_extractor", None)
            from memory_extractor import has_trigger
            msgs = [
                {"role": "user", "text": "이거 fix"},
                {
                    "role": "assistant",
                    "text": "fix 했어",
                    "bash_commands": ["git commit"],
                },
                {"role": "user", "text": "좋아!"},
            ]
            self.assertFalse(
                has_trigger(msgs),
                "ACK trigger OFF env → NEXT-10 분기 비활성",
            )
        # 다른 테스트가 영향받지 않도록 reload (default ON)
        sys.modules.pop("memory_extractor", None)


class TestExistingTriggersStillWork(unittest.TestCase):
    """기존 NEXT-1, TRIGGER_RE layer 회귀 안 함 (NEXT-10 추가가 기존 분기 안 깼는지)."""

    def test_keyword_trigger_still_works(self):
        from memory_extractor import has_trigger
        msgs = [{"role": "user", "text": "이 명령어 외워둬: claude --bg"}]
        self.assertTrue(has_trigger(msgs))

    def test_next1_special_bash_plus_next_action(self):
        from memory_extractor import has_trigger
        msgs = [
            {"role": "user", "text": "launchctl 로 영구화 하자"},
            {
                "role": "assistant",
                "text": "plist 만들고 load 했어",
                "bash_commands": ["launchctl load -w ~/Library/LaunchAgents/foo.plist"],
            },
            {"role": "user", "text": "영구화 적용"},
        ]
        self.assertTrue(has_trigger(msgs))


if __name__ == "__main__":
    unittest.main()
