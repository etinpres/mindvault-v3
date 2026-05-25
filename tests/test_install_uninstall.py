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


class TestCloseSessionSkillDeploy(unittest.TestCase):
    """audit-2026-05-25: close-session/cs 스킬을 install.sh 가 deploy 하는지
    + uninstall.sh 가 정리하는지 + skill 본문 자체 회귀 가드."""

    def setUp(self):
        self.repo = Path(__file__).resolve().parent.parent

    def test_skill_source_files_exist(self):
        """skill/close-session.md + skill/cs.md repo 에 존재."""
        self.assertTrue((self.repo / "skill" / "close-session.md").exists())
        self.assertTrue((self.repo / "skill" / "cs.md").exists())

    def test_install_wires_close_session(self):
        """install.sh 가 두 skill target 을 모두 명시."""
        sh = (self.repo / "install.sh").read_text()
        self.assertIn("close-session.md", sh)
        self.assertIn("cs.md", sh)
        # COMMANDS_DIR 경유로 deploy 되는지
        self.assertIn("CLOSE_SESSION_SKILL_TARGET", sh)
        self.assertIn("CS_SKILL_TARGET", sh)

    def test_uninstall_removes_close_session(self):
        """uninstall.sh 가 두 skill 모두 제거."""
        sh = (self.repo / "uninstall.sh").read_text()
        self.assertIn("close-session", sh)
        # for-loop 한 줄에 두 스킬 다 잡히는지
        self.assertIn("close-session cs", sh)

    def test_skill_has_no_hardcoded_user_path(self):
        """audit Critical #1 회귀 가드 — hardcoded user-specific path 금지.

        skill 본문은 일반 사용자도 작동해야 하므로 특정 사용자 home/slug 박지 말 것.
        """
        body = (self.repo / "skill" / "close-session.md").read_text()
        # 특정 사용자명 leak 차단
        forbidden = ["yonghaekim", "/Users/yonghaekim", "dr.ocean", "vibe1977"]
        for tok in forbidden:
            self.assertNotIn(
                tok, body,
                f"skill/close-session.md 안에 personal identifier '{tok}' — sanitize 필요"
            )

    def test_skill_supports_procedural_type(self):
        """audit High #4 회귀 가드 — 5개 type 모두 enum 에 포함."""
        body = (self.repo / "skill" / "close-session.md").read_text()
        for type_name in ("user", "feedback", "project", "reference", "procedural"):
            self.assertIn(type_name, body, f"type '{type_name}' 누락")

    def test_skill_has_concurrency_guard(self):
        """audit codex Medium 회귀 가드 — flock + atomic rename 가드 명시."""
        body = (self.repo / "skill" / "close-session.md").read_text()
        self.assertIn("flock", body, "동시성 lock 가드 누락")
        self.assertIn("mktemp", body, "atomic rename pattern 누락")

    def test_skill_has_slug_sanitize(self):
        """audit codex High 회귀 가드 — slug sanitize regex 명시."""
        body = (self.repo / "skill" / "close-session.md").read_text()
        self.assertIn("[a-z]", body, "slug sanitize regex 누락")
        self.assertIn("realpath", body, "path traversal defense in-depth 누락")

    def test_cs_alias_redirects_to_close_session(self):
        """cs 는 close-session 본문 Read 만 하는 single-source-of-truth 패턴."""
        cs = (self.repo / "skill" / "cs.md").read_text()
        self.assertIn("close-session.md", cs)
        self.assertIn("Read", cs)

    def test_skill_validates_mem_dir_boundary(self):
        """codex round-2 N5 가드 — MV3_MEMORY_DIR override 시 PROJECTS_ROOT 하위 강제."""
        body = (self.repo / "skill" / "close-session.md").read_text()
        self.assertIn("_validate_mem_dir", body, "boundary validator 함수 누락")
        self.assertIn("PROJECTS_ROOT", body, "PROJECTS_ROOT 상수 누락")

    def test_skill_uses_nul_separated_find(self):
        """codex round-2 N6 가드 — find -print0 패턴 (whitespace path 안전)."""
        body = (self.repo / "skill" / "close-session.md").read_text()
        self.assertIn("-print0", body, "NUL-separated find 누락")
        self.assertIn("xargs -0", body, "xargs -0 누락")

    def test_skill_has_lock_cleanup(self):
        """codex round-2 N1 가드 — lock fd 해제 + lock 파일 정리 (trap)."""
        body = (self.repo / "skill" / "close-session.md").read_text()
        self.assertIn("trap", body, "trap (EXIT) lock cleanup 누락")
        self.assertIn('rm -f "$LOCK_FILE"', body, "lock 파일 정리 누락")

    def test_skill_detects_slug_race(self):
        """codex round-2 N2 가드 — 같은 슬러그 race 시 silent loss 안 함."""
        body = (self.repo / "skill" / "close-session.md").read_text()
        self.assertIn("conflict-", body, "slug race rescue 패턴 누락")
        self.assertNotIn("mv -n ", body, "mv -n (silent loss) 패턴 잔존")

    def test_install_uses_array_not_colon_triple(self):
        """codex round-2 N7 가드 — install.sh 가 ':'-delim triple 대신 array 사용."""
        sh = (self.repo / "install.sh").read_text()
        self.assertIn("SKILL_TRIPLES=(", sh, "array-based skill loop 누락")

    def test_uninstall_uses_unique_marker(self):
        """codex round-3 D 가드 — uninstall sentinel 이 unique `[mv3-skill]` 마커.

        round-2 의 'MindVault v3' 매칭은 사용자 customize 본문에도 자주 박혀 false-positive
        delete 위험. round-3 에서 frontmatter description 첫머리 `[mv3-skill]` 토큰으로 교체.
        """
        sh = (self.repo / "uninstall.sh").read_text()
        self.assertIn("[mv3-skill]", sh, "uninstall unique marker 매칭 누락")
        # 두 skill frontmatter 에 marker 실존
        for name in ("close-session.md", "cs.md"):
            body = (self.repo / "skill" / name).read_text()
            self.assertIn("[mv3-skill]", body, f"{name} 에 [mv3-skill] marker 없음")

    def test_validator_rejects_projects_root_itself(self):
        """codex round-3 C 가드 — _validate_mem_dir 가 PROJECTS_ROOT 자체는 거부."""
        body = (self.repo / "skill" / "close-session.md").read_text()
        # root 자체 거부 명시
        self.assertIn('[ "$abs" = "$root" ] && return 1', body,
                      "PROJECTS_ROOT 자체 거부 가드 누락")


if __name__ == "__main__":
    unittest.main()
