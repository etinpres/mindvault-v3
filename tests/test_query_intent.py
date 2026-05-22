"""Sprint 16 — Query Intent Classifier 단위 테스트."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))


class TestClassify(unittest.TestCase):
    def test_chat_greetings(self):
        from query_intent import classify
        for q in [
            "안녕하세요",
            "굿모닝!",
            "오늘 날씨 어때",
            "오늘 점심 뭐 먹지",
            "고마워요",
            "잘자",
        ]:
            r = classify(q)
            self.assertEqual(r.intent, "chat", f"{q!r} → {r}")

    def test_chat_short_fallback(self):
        from query_intent import classify
        # 짧고 단어 적음 → chat fallback
        r = classify("ㅎㅇ")
        self.assertEqual(r.intent, "chat")
        self.assertIn("short-fallback", r.matched)

    def test_meta(self):
        from query_intent import classify
        for q in [
            "너는 어떤 모델이야?",
            "context 얼마 남았어",
            "토큰 얼마 사용했어?",
            "현재 세션 정보",
            "claude code 버전이 뭐야",
        ]:
            r = classify(q)
            self.assertEqual(r.intent, "meta", f"{q!r} → {r}")

    def test_code(self):
        from query_intent import classify
        for q in [
            "이 함수 고쳐줘",
            "테스트 돌려봐",
            "src/memory_indexer.py 의 _collect_md_files 변경",
            "이 버그 fix 해",
            "commit 해줘",
            "PR 만들어줘",
            "타입 체크 돌려봐",
        ]:
            r = classify(q)
            self.assertEqual(r.intent, "code", f"{q!r} → {r}")

    def test_recall(self):
        from query_intent import classify
        for q in [
            "예전에 했던 얘기 뭐였지",
            "지난번에 어떤 모델 썼더라",
            "이전에 합의했던 거 기억나",
            "그때 만든 거 뭐였어?",
            "옛날에 어떻게 했지",
        ]:
            r = classify(q)
            self.assertEqual(r.intent, "recall", f"{q!r} → {r}")

    def test_unknown(self):
        from query_intent import classify
        # 명확한 의도 카테고리 없음 — 도메인 query 등
        for q in [
            "MindVault Sprint 진행 상황",
            "Arctic-ko 임베딩 분포",
            "임베딩 서버 응답 분석",
        ]:
            r = classify(q)
            self.assertEqual(r.intent, "unknown", f"{q!r} → {r}")

    def test_priority_recall_over_code(self):
        """recall + code 키워드 동시 — recall 우선."""
        from query_intent import classify
        r = classify("예전에 이 함수 고쳤었지")
        self.assertEqual(r.intent, "recall")

    def test_priority_code_over_meta(self):
        """code + meta — code 우선 (작업 의도)."""
        from query_intent import classify
        r = classify("claude code 라는 모델로 이 함수 고쳐")
        # 'claude code' 가 meta 매칭이지만 '이 함수 고쳐' 가 code 매칭 → recall 없음 → code 우선
        self.assertEqual(r.intent, "code")


class TestShouldSkipRecall(unittest.TestCase):
    def test_skip_chat_meta(self):
        from query_intent import IntentResult, should_skip_recall
        self.assertTrue(
            should_skip_recall(IntentResult("chat", 0.8, []))
        )
        self.assertTrue(
            should_skip_recall(IntentResult("meta", 0.9, []))
        )

    def test_keep_others(self):
        from query_intent import IntentResult, should_skip_recall
        for intent in ("recall", "code", "unknown"):
            self.assertFalse(
                should_skip_recall(IntentResult(intent, 0.5, [])),
                f"잘못된 skip: {intent}",
            )
