"""eval_arctic_ko_ab.py 안전 가드 회귀 (Sprint 14 BGE-M3 deprecate 대응).

옛 코드는 BGE_M3_URL/ARCTIC_KO_URL 이 같은 포트(둘 다 8081) 라도 무비판적으로
A/B 비교 돌려 false 결과를 출력했다. 새 코드는 같은 URL / dead 서버일 때 빠르게
sys.exit(2/3) 한다.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


class TestEvalGuard(unittest.TestCase):
    def test_same_url_exits_with_code_2(self):
        # 환경변수로 두 URL 동일하게 만들면 run_ab() 가 exit 2
        os.environ["MV3_EVAL_BGE_M3_URL"] = "http://localhost:8081/embed"
        os.environ["MV3_EVAL_ARCTIC_KO_URL"] = "http://localhost:8081/embed"
        # module reload 로 환경변수 반영
        if "eval_arctic_ko_ab" in sys.modules:
            del sys.modules["eval_arctic_ko_ab"]
        import eval_arctic_ko_ab as mod

        with patch.object(mod, "collect_memory_files", return_value=[]):
            with self.assertRaises(SystemExit) as ctx:
                mod.run_ab()
            self.assertEqual(ctx.exception.code, 2)

    def test_dead_server_exits_with_code_3(self):
        os.environ["MV3_EVAL_BGE_M3_URL"] = "http://localhost:59991/embed"
        os.environ["MV3_EVAL_ARCTIC_KO_URL"] = "http://localhost:59992/embed"
        if "eval_arctic_ko_ab" in sys.modules:
            del sys.modules["eval_arctic_ko_ab"]
        import eval_arctic_ko_ab as mod

        with patch.object(mod, "collect_memory_files", return_value=[]):
            with self.assertRaises(SystemExit) as ctx:
                mod.run_ab()
            self.assertEqual(ctx.exception.code, 3)

    def tearDown(self):
        for k in ("MV3_EVAL_BGE_M3_URL", "MV3_EVAL_ARCTIC_KO_URL"):
            os.environ.pop(k, None)
        if "eval_arctic_ko_ab" in sys.modules:
            del sys.modules["eval_arctic_ko_ab"]


class TestArcticServerBrokenPipeHandling(unittest.TestCase):
    """v3.2.6 L1: arctic_ko_server._send_json 가 client disconnect 시 silent log.

    이전엔 hook timeout 으로 client 가 socket 끊으면 except 블록의 _send_json
    이 broken socket 에 write 시도해 BrokenPipeError traceback 가 err.log 에
    누적 (~24건/2061라인). source-level 회귀 차단.
    """

    def test_send_json_catches_broken_pipe(self):
        src_path = Path(__file__).parent.parent / "scripts" / "arctic_ko_server.py"
        text = src_path.read_text()
        snd_idx = text.index("def _send_json(")
        next_def = text.find("\n    def ", snd_idx + 1)
        body = text[snd_idx:next_def if next_def != -1 else None]
        self.assertIn("BrokenPipeError", body)
        self.assertIn("ConnectionResetError", body)


if __name__ == "__main__":
    unittest.main()
