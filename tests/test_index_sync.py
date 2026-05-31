"""NEXT-35 — session_memory_end.py 가 SessionEnd 시 memory_indexer.incremental_index
를 호출하는지 회귀 가드. v3.2.9 fix.

배경: v3.2.5 NEXT-34 가 alias_index 자동 동기화를 도입했지만 memories/_fts/_vec
테이블 sync 는 install.sh 1회 실행 의존이라, 새 .md 추가 후 install.sh 재실행
안 하면 영원히 stale. recall 시 raw cosine 게이트가 통과해도 발췌가 옛 위치를
가리키거나 (snippet window 오작동 증상), 신규 reference 메모리는 vec/fts/alias
모두 누락. 코드 주석은 "alias_index 자동 동기화"라 일관됐는데 memories 테이블
sync 누락 사실이 노출 안 된 self-affirming 결함 family.

검증 3축:
1. static: 본문에 incremental_index import + 호출 코드가 살아있나
2. behavior: main() 실행 시 incremental_index 가 실제 호출되나 (mock spy)
3. silent: incremental_index 가 Exception raise 해도 main 이 죽지 않나
"""
import importlib.util
import io
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

_WT_SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(_WT_SRC))


def _load_session_memory_end():
    """worktree 본 명시 로드 — production deploy 본 sys.modules 캐싱 회피
    ([[procedural-env-var-test-isolation]] 패턴).
    """
    spec = importlib.util.spec_from_file_location(
        "session_memory_end_wt",
        _WT_SRC / "session_memory_end.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestStaticIndexSyncWiring(unittest.TestCase):
    """source-level grep — refactor 시 호출 라인 silent drop 회귀 가드."""

    def setUp(self):
        self.src = (_WT_SRC / "session_memory_end.py").read_text(encoding="utf-8")

    def test_imports_incremental_index(self):
        self.assertIn(
            "from memory_indexer import incremental_index",
            self.src,
            "session_memory_end 가 memory_indexer.incremental_index 를 import 하지 않음 "
            "— NEXT-35 회귀",
        )

    def test_invokes_index_memories(self):
        # alias 이름은 _index_memories — 호출 라인이 살아있어야 의미 있음
        self.assertIn(
            "_index_memories()",
            self.src,
            "session_memory_end 가 _index_memories() 호출 라인을 잃음 — NEXT-35 회귀",
        )

    def test_index_sync_call_precedes_alias_sync(self):
        """memories 테이블 채운 후 alias_generator 가 그 위에 alias 생성하도록
        호출 순서 강제. 두 호출 위치를 본문에서 찾아 line 비교.
        """
        i_idx = self.src.find("_index_memories()")
        i_alias = self.src.find("_alias_generate(purge_missing=True)")
        self.assertGreater(i_idx, 0, "_index_memories() 호출 미발견")
        self.assertGreater(i_alias, 0, "_alias_generate() 호출 미발견")
        self.assertLess(
            i_idx, i_alias,
            "_index_memories() 호출이 _alias_generate 보다 뒤 — 순서 회귀",
        )


def _stub_main_dependencies(mod, *, index_side_effect=None, index_return=None):
    """main() 의 jsonl/compile/write 의존성을 stub 하고 incremental_index 만 spy.

    main() 본문에서 session_id 는 stdin + env 로 얻으므로 양쪽 다 패치한다.
    candidates 1건 주입해 index_sync/alias_sync 블록 도달까지 흐른다.
    반환: (index_spy, debug_mock, alias_spy)
    """
    fake_sid = "deadbeef-cafe-1234-5678-abcdef012345"
    fake_jsonl = Path("/tmp/fake-mv3-test.jsonl")
    proot_mock = MagicMock()
    proot_mock.glob.return_value = [fake_jsonl]

    if index_side_effect is not None:
        spy = patch("memory_indexer.incremental_index",
                    side_effect=index_side_effect)
    else:
        spy = patch(
            "memory_indexer.incremental_index",
            return_value=(index_return
                          or {"updated": 7, "skipped": 100, "removed": 0}),
        )
    return spy, fake_sid, proot_mock, fake_jsonl


class TestBehaviorIndexSyncCalled(unittest.TestCase):
    """main() 실행 시 memory_indexer.incremental_index 가 실제 호출되는지 mock spy."""

    def test_main_calls_incremental_index(self):
        mod = _load_session_memory_end()
        spy, fake_sid, proot_mock, _ = _stub_main_dependencies(mod)

        with patch.object(mod, "_debug"), \
             patch.object(mod.sys, "stdin", io.StringIO("")), \
             patch.dict(os.environ, {"CLAUDE_SESSION_ID": fake_sid}, clear=False), \
             patch.object(mod, "PROJECTS_ROOT", proot_mock), \
             patch.object(mod, "extract_from_jsonl",
                          return_value=[{"slug": "x", "body": "y"}]), \
             patch.object(mod, "existing_slugs", return_value=set()), \
             patch.object(mod, "_stage_with_conflict_resolution",
                          return_value=1), \
             patch("alias_generator.generate",
                   return_value={"generated": 0, "purged": 0, "failed": 0}), \
             spy as index_spy:
            mod.main()

        self.assertTrue(
            index_spy.called,
            "main() 가 memory_indexer.incremental_index 를 호출하지 않음 — NEXT-35 회귀",
        )


class TestSilentFailure(unittest.TestCase):
    """index_sync 가 Exception raise 해도 main() 이 죽지 않고 alias_sync 로 진행."""

    def test_index_sync_failure_silent(self):
        mod = _load_session_memory_end()
        spy, fake_sid, proot_mock, _ = _stub_main_dependencies(
            mod, index_side_effect=RuntimeError("simulated indexer fail"),
        )

        with patch.object(mod, "_debug") as dbg, \
             patch.object(mod.sys, "stdin", io.StringIO("")), \
             patch.dict(os.environ, {"CLAUDE_SESSION_ID": fake_sid}, clear=False), \
             patch.object(mod, "PROJECTS_ROOT", proot_mock), \
             patch.object(mod, "extract_from_jsonl",
                          return_value=[{"slug": "x", "body": "y"}]), \
             patch.object(mod, "existing_slugs", return_value=set()), \
             patch.object(mod, "_stage_with_conflict_resolution",
                          return_value=1), \
             patch("alias_generator.generate",
                   return_value={"generated": 0, "purged": 0, "failed": 0}) as alias_spy, \
             spy:
            rc = mod.main()

        self.assertEqual(rc, 0, "index_sync 실패 시 main() 종료코드 0 유지")
        skipped_logged = any(
            "index_sync skipped" in str(call_args)
            for call_args in dbg.call_args_list
        )
        self.assertTrue(
            skipped_logged,
            "_debug 에 'index_sync skipped' 로깅 누락 — silent 처리 흔적 부재",
        )
        self.assertTrue(
            alias_spy.called,
            "index_sync 실패 후 alias_sync 가 호출되지 않음 — try 블록 격리 실패",
        )


class TestReverifyNotGatedByCandidates(unittest.TestCase):
    """audit sweep R1 — 주1회 reverify 가 'if not candidates: return 0' 에 갇혀
    no-candidate 세션(흔함)에서 cadence 가 starve 되면 안 된다."""

    def test_reverify_fires_on_no_candidate_session(self):
        mod = _load_session_memory_end()
        fake_sid = "deadbeef-cafe-1234-5678-abcdef012345"
        proot_mock = MagicMock()
        proot_mock.glob.return_value = [Path("/tmp/fake-mv3-test.jsonl")]
        with patch.object(mod, "_debug"), \
             patch.object(mod.sys, "stdin", io.StringIO("")), \
             patch.dict(os.environ, {"CLAUDE_SESSION_ID": fake_sid}, clear=False), \
             patch.object(mod, "PROJECTS_ROOT", proot_mock), \
             patch.object(mod, "extract_from_jsonl", return_value=[]), \
             patch("reverify.maybe_scan_due", return_value=None) as msd:
            rc = mod.main()
        self.assertEqual(rc, 0)
        self.assertTrue(
            msd.called,
            "no-candidate 세션에서 reverify(maybe_scan_due) 미호출 — early-return 결합 회귀",
        )


if __name__ == "__main__":
    unittest.main()
