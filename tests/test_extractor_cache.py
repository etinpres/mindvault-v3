"""Sprint NEXT-16 — extractor prompt → candidates 결과 캐시.

검증 대상:
- prompt_hash: 같은 input 같은 hash, 다른 input 다른 hash
- cache_enabled: default true, MV2_EXTRACTOR_CACHE_DISABLE=1 false
- cache_put + cache_get round-trip (빈 list 도 저장)
- cache_get hit 시 hit_count 증가
- cache_clear / cache_stats
- extract_from_jsonl 첫 호출 Gemma + cache_put, 두 번째 cache hit (Gemma 안 호출)
- MV2_EXTRACTOR_CACHE_DISABLE=1 → 매번 Gemma 재호출
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

for _mod in ("extractor_cache", "memory_extractor"):
    sys.modules.pop(_mod, None)


class CacheTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_db = Path(self.tmp.name) / "test_cache.db"
        # module 재로드 + CACHE_DB monkey-patch
        for m in ("extractor_cache", "memory_extractor"):
            sys.modules.pop(m, None)
        import extractor_cache as ec
        ec.CACHE_DB = self.tmp_db
        ec._initialized = False
        self.ec = ec
        # default env: cache enabled
        os.environ.pop("MV2_EXTRACTOR_CACHE_DISABLE", None)

    def tearDown(self):
        self.tmp.cleanup()


class TestPromptHash(CacheTestBase):
    def test_same_input_same_hash(self):
        self.assertEqual(self.ec.prompt_hash("foo"), self.ec.prompt_hash("foo"))

    def test_different_input_different_hash(self):
        self.assertNotEqual(
            self.ec.prompt_hash("foo"), self.ec.prompt_hash("bar")
        )

    def test_hash_is_sha256_hex(self):
        h = self.ec.prompt_hash("x")
        self.assertEqual(len(h), 64)
        int(h, 16)  # valid hex


class TestCacheEnabled(CacheTestBase):
    def test_default_enabled(self):
        self.assertTrue(self.ec.cache_enabled())

    def test_disable_env(self):
        os.environ["MV2_EXTRACTOR_CACHE_DISABLE"] = "1"
        try:
            self.assertFalse(self.ec.cache_enabled())
        finally:
            os.environ.pop("MV2_EXTRACTOR_CACHE_DISABLE", None)


class TestPutGet(CacheTestBase):
    def test_round_trip(self):
        candidates = [
            {"type": "procedural", "title": "X", "body": "x"},
            {"type": "project", "title": "Y", "body": "y"},
        ]
        self.ec.cache_put("prompt-1", candidates)
        got = self.ec.cache_get("prompt-1")
        self.assertEqual(got, candidates)

    def test_get_miss_returns_none(self):
        self.assertIsNone(self.ec.cache_get("never-stored"))

    def test_put_empty_list_is_cached(self):
        """0건 결과도 캐싱 — 재시도 Gemma 호출 비용 회피."""
        self.ec.cache_put("prompt-empty", [])
        got = self.ec.cache_get("prompt-empty")
        self.assertEqual(got, [])
        self.assertIsNotNone(got, "miss 와 빈 hit 구분")

    def test_hit_count_increments(self):
        self.ec.cache_put("prompt-count", [{"type": "feedback", "title": "A", "body": "a"}])
        self.ec.cache_get("prompt-count")
        self.ec.cache_get("prompt-count")
        self.ec.cache_get("prompt-count")
        stats = self.ec.cache_stats()
        self.assertEqual(stats["total_hits"], 3)

    def test_disable_env_makes_get_return_none(self):
        self.ec.cache_put("p", [{"type": "feedback", "title": "z", "body": "z"}])
        os.environ["MV2_EXTRACTOR_CACHE_DISABLE"] = "1"
        try:
            self.assertIsNone(self.ec.cache_get("p"))
        finally:
            os.environ.pop("MV2_EXTRACTOR_CACHE_DISABLE", None)

    def test_disable_env_makes_put_noop(self):
        os.environ["MV2_EXTRACTOR_CACHE_DISABLE"] = "1"
        try:
            self.ec.cache_put("p", [{"type": "feedback", "title": "z", "body": "z"}])
        finally:
            os.environ.pop("MV2_EXTRACTOR_CACHE_DISABLE", None)
        # 재 활성화 후 조회 → 없음
        self.assertIsNone(self.ec.cache_get("p"))


class TestStats(CacheTestBase):
    def test_empty_stats(self):
        stats = self.ec.cache_stats()
        self.assertEqual(stats["entries"], 0)
        self.assertEqual(stats["total_hits"], 0)

    def test_stats_after_puts(self):
        self.ec.cache_put("a", [{"type": "feedback", "title": "1", "body": "1"}])
        self.ec.cache_put("b", [{"type": "feedback", "title": "2", "body": "2"},
                                 {"type": "project", "title": "3", "body": "3"}])
        stats = self.ec.cache_stats()
        self.assertEqual(stats["entries"], 2)
        self.assertEqual(stats["total_candidates"], 3)

    def test_clear(self):
        self.ec.cache_put("a", [{"type": "feedback", "title": "x", "body": "x"}])
        self.ec.cache_put("b", [{"type": "feedback", "title": "y", "body": "y"}])
        removed = self.ec.cache_clear()
        self.assertEqual(removed, 2)
        self.assertEqual(self.ec.cache_stats()["entries"], 0)


class TestExtractIntegration(CacheTestBase):
    def _make_jsonl(self, tmp: Path) -> Path:
        path = tmp / "test-sid.jsonl"
        lines = [
            {"type": "user", "message": {"content": "이거 해줘"}},
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Bash",
                         "input": {"command": "launchctl load -w foo.plist"}},
                    ]
                },
            },
            {"type": "user", "message": {"content": "영구화 적용"}},
        ]
        path.write_text("\n".join(json.dumps(l) for l in lines))
        return path

    def test_first_call_invokes_gemma_then_caches(self):
        call_count = {"n": 0}

        def fake_gemma(prompt, **kw):
            call_count["n"] += 1
            return '[{"type":"procedural","title":"X","body":"x","reason":"r","evidence":"e"}]'

        with tempfile.TemporaryDirectory() as tmp:
            jsonl = self._make_jsonl(Path(tmp))
            with patch.dict(os.environ, {"MV2_EXTRACTOR_GEMMA_RETRIES": "0"}):
                sys.modules.pop("memory_extractor", None)
                import memory_extractor as me
                with patch.object(me, "call_gemma", side_effect=fake_gemma):
                    out1 = me.extract_from_jsonl(jsonl)
                    out2 = me.extract_from_jsonl(jsonl)
            self.assertEqual(out1, out2)
            self.assertEqual(
                call_count["n"], 1,
                "두 번째 호출은 cache hit → Gemma 미호출"
            )

    def test_disable_cache_invokes_gemma_each_time(self):
        call_count = {"n": 0}

        def fake_gemma(prompt, **kw):
            call_count["n"] += 1
            return '[{"type":"procedural","title":"Y","body":"y","reason":"r","evidence":"e"}]'

        with tempfile.TemporaryDirectory() as tmp:
            jsonl = self._make_jsonl(Path(tmp))
            with patch.dict(os.environ, {
                "MV2_EXTRACTOR_GEMMA_RETRIES": "0",
                "MV2_EXTRACTOR_CACHE_DISABLE": "1",
            }):
                sys.modules.pop("memory_extractor", None)
                import memory_extractor as me
                with patch.object(me, "call_gemma", side_effect=fake_gemma):
                    me.extract_from_jsonl(jsonl)
                    me.extract_from_jsonl(jsonl)
            self.assertEqual(
                call_count["n"], 2,
                "CACHE_DISABLE=1 → 매번 Gemma 호출"
            )


if __name__ == "__main__":
    unittest.main()
