"""audit-2026-05-24 Fix #1 회귀 가드 — `src/search.py:fts_escape` 단위.

sessions 검색 경로의 FTS5 쿼리 정책이 `memory_search._fts_escape` 와
동일하게 유지되는지 확인. 한글/영문/숫자 화이트리스트, 단독 숫자 제외,
2자 이상, prefix wildcard, OR 결합.

NOTE: test_memory_hook 가 production deploy 경로
(~/.claude/scripts/mindvault) 를 sys.path 첫 자리에 insert 해 search 가
production(옛 버전) 으로 캐싱되는 케이스 회피 — worktree src/search.py
파일을 명시적 spec loader 로 별도 모듈명 `_worktree_search` 로 로드.
"""
import importlib.util
import sqlite3
import sys
import unittest
from pathlib import Path


def _load_worktree_search():
    path = Path(__file__).parent.parent / "src" / "search.py"
    spec = importlib.util.spec_from_file_location("_worktree_search", path)
    mod = importlib.util.module_from_spec(spec)
    # search.py 의 `from memory_indexer import ...` 같은 import 를 위해
    # src 디렉토리를 sys.path 첫 자리에 임시 추가.
    src_dir = str(path.parent)
    inserted = src_dir not in sys.path
    if inserted:
        sys.path.insert(0, src_dir)
    try:
        spec.loader.exec_module(mod)
    finally:
        if inserted and src_dir in sys.path:
            sys.path.remove(src_dir)
    return mod


search = _load_worktree_search()
fts_escape = search.fts_escape


class TestSearchFtsEscape(unittest.TestCase):
    def test_alnum_only_with_prefix_wildcard(self):
        
        # 한글 + 영문 + 숫자 토큰 → 각 `*` prefix, OR 결합.
        self.assertEqual(fts_escape("스캐너 동작"), "스캐너* OR 동작*")
        self.assertEqual(fts_escape("L007 영상"), "L007* OR 영상*")

    def test_bare_number_dropped(self):
        
        # 단독 숫자는 column 참조로 해석되니 제외, 다른 토큰만 살아남음.
        self.assertEqual(fts_escape("33 진행"), "진행*")

    def test_single_char_dropped(self):
        
        # 1자 토큰(한국어 조사·noise) 제외.
        self.assertEqual(fts_escape("a 스캐너"), "스캐너*")

    def test_special_chars_stripped(self):
        
        # `.~?/-:` 특수문자는 token break 후 무시.
        # (`http`, `x`, `com` 셋 중 `x` 는 1자라 drop)
        result = fts_escape("http://x.com/?q=hi")
        # 토큰: http, x, com, q, hi → 2자+ 면 http/com/hi.
        # 순서: re.findall 순서 보존.
        self.assertEqual(result, "http* OR com* OR hi*")

    def test_empty_query_returns_safe_token(self):
        
        # 모든 토큰이 무효 (공백/단독숫자/특수문자만) → 빈 매치 안전 토큰.
        self.assertEqual(fts_escape(""), '""')
        self.assertEqual(fts_escape("   "), '""')
        self.assertEqual(fts_escape("33 8"), '""')  # 둘 다 단독 숫자
        self.assertEqual(fts_escape("?"), '""')

    def test_fts5_match_accepts_output(self):
        """실제 FTS5 MATCH 가 출력 토큰 받는지 — syntax error 회귀 가드."""
        

        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE VIRTUAL TABLE t USING fts5(body, tokenize='unicode61')"
        )
        conn.execute("INSERT INTO t(body) VALUES ('스캐너 동작 안 함')")
        conn.execute("INSERT INTO t(body) VALUES ('33번 sprint 진행')")
        conn.commit()

        for q in [
            "스캐너 동작",
            "L007 영상",
            "33 진행",
            "http://example.com/?q=test",
        ]:
            with self.subTest(query=q):
                fts_q = fts_escape(q)
                # 빈 토큰 매칭은 0건 정상, 실패는 raise.
                conn.execute(
                    "SELECT count(*) FROM t WHERE t MATCH ?", (fts_q,)
                ).fetchone()


if __name__ == "__main__":
    unittest.main()
