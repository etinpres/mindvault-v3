"""Sprint 4 Task 7 — install/uninstall settings.json 변형 검증."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


# install.sh 4.7 섹션의 idempotent append 로직을 재현 (스크립트 분리 테스트용)
INSTALL_REGISTER_CODE = '''
import json, sys
from pathlib import Path

hook_cmd = sys.argv[1]
settings_path = Path(sys.argv[2])
data = json.loads(settings_path.read_text())
hooks = data.setdefault("hooks", {})
ups = hooks.setdefault("UserPromptSubmit", [])

new_hook = {
    "matcher": "*",
    "hooks": [{"type": "command", "command": hook_cmd}],
}
already = any("memory-recall.py" in json.dumps(h) for h in ups)
if not already:
    ups.append(new_hook)
    settings_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\\n")
    print("appended")
else:
    print("skipped")
'''


class TestSettingsJsonInstall(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Path(self.tmp.name) / "settings.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _run_install(self, hook_cmd: str = "/home/test/hooks/memory-recall.py"):
        r = subprocess.run(
            [sys.executable, "-c", INSTALL_REGISTER_CODE, hook_cmd, str(self.settings)],
            capture_output=True,
            check=True,
        )
        return r.stdout.decode().strip()

    def test_appends_to_existing_hooks(self):
        """기존 telegram-guard 같은 hook 보존하면서 추가."""
        self.settings.write_text(json.dumps({
            "hooks": {
                "UserPromptSubmit": [
                    {"matcher": "*", "hooks": [{"type": "command", "command": "/existing/telegram-guard.sh"}]}
                ]
            }
        }, indent=2))
        self.assertEqual(self._run_install(), "appended")
        data = json.loads(self.settings.read_text())
        ups = data["hooks"]["UserPromptSubmit"]
        self.assertEqual(len(ups), 2)
        # 둘 다 보존
        self.assertTrue(any("telegram-guard" in json.dumps(h) for h in ups))
        self.assertTrue(any("memory-recall" in json.dumps(h) for h in ups))

    def test_idempotent(self):
        """두 번 실행해도 hook은 1개만."""
        self.settings.write_text(json.dumps({"hooks": {}}, indent=2))
        self.assertEqual(self._run_install(), "appended")
        self.assertEqual(self._run_install(), "skipped")
        data = json.loads(self.settings.read_text())
        ups = data["hooks"]["UserPromptSubmit"]
        self.assertEqual(len(ups), 1)

    def test_creates_hooks_key_if_missing(self):
        """빈 settings.json도 처리."""
        self.settings.write_text("{}")
        self.assertEqual(self._run_install(), "appended")
        data = json.loads(self.settings.read_text())
        self.assertIn("UserPromptSubmit", data["hooks"])

    def test_install_script_syntax(self):
        """install.sh / uninstall.sh 문법 검증."""
        repo = Path(__file__).parent.parent
        for sh in ("install.sh", "uninstall.sh"):
            r = subprocess.run(
                ["bash", "-n", str(repo / sh)],
                capture_output=True,
            )
            self.assertEqual(r.returncode, 0, f"{sh}: {r.stderr.decode()}")


if __name__ == "__main__":
    unittest.main()
