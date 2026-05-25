"""Sprint 4 Task 7 — install/uninstall settings.json 변형 검증."""
import json
import os
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

    def test_install_uses_defensive_deploy_skill(self):
        """v3.2.2 가드 — install.sh 가 deploy_skill 헬퍼로 cp 사이트 통일.

        옛 SKILL_TRIPLES 배열 + 직접 cp 패턴은 cp fail 시 .bak 만 남고 .md 잃을 위험.
        v3.2.2 의 deploy_skill 헬퍼는 cp fail 시 .bak 자동 복원 보장.
        """
        sh = (self.repo / "install.sh").read_text()
        self.assertIn("deploy_skill()", sh, "deploy_skill 헬퍼 정의 누락")
        # 4개 skill 모두 헬퍼 호출 — recall, memory_review, close-session, cs
        for label in ("/recall", "/memory_review", "/close-session", "/cs"):
            self.assertIn(f'deploy_skill ', sh, f"{label} deploy_skill 호출 누락")
        # 옛 SKILL_TRIPLES 배열 패턴은 제거됨
        self.assertNotIn("SKILL_TRIPLES=(", sh, "옛 SKILL_TRIPLES 배열 잔존 (deploy_skill 로 교체됐어야)")

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


class TestV3_1_1PostShipFixes(unittest.TestCase):
    """v3.1.1 audit-2026-05-25 post-ship dogfood + codex round-5 fix 회귀 가드."""

    def setUp(self):
        self.repo = Path(__file__).resolve().parent.parent

    def test_install_handles_personal_skill_conflict(self):
        """CRITICAL #1 가드 — install.sh 가 옛 personal `~/.claude/skills/{cs,close-session}/`
        디렉토리를 발견하면 sentinel 검사 후 백업."""
        sh = (self.repo / "install.sh").read_text()
        self.assertIn("skills/close-session", sh, "personal skill conflict 처리 누락")
        self.assertIn("skills.attic", sh, "백업 디렉토리 패턴 누락")

    def test_cwd_slot_activity_heuristic(self):
        """HIGH #2 가드 — cwd 슬롯 priority 2 가 .md ≥ 5 휴리스틱 통과 시에만 채택."""
        body = (self.repo / "skill" / "close-session.md").read_text()
        self.assertIn("md_count", body, "활성 슬롯 휴리스틱 누락")
        self.assertIn("-ge 5", body, ".md >= 5 게이트 누락")

    def test_dry_run_arg_parsing_spec(self):
        """HIGH #3 가드 — --dry-run 인자 surface 명세 추가."""
        body = (self.repo / "skill" / "close-session.md").read_text()
        self.assertIn("$ARGUMENTS", body, "args surface 명세 누락")
        self.assertIn("DRY_RUN=1", body, "DRY_RUN flag 누락")

    def test_flock_edit_tool_gap_documented(self):
        """HIGH #4 가드 — flock vs Edit/Write tool gap 명시."""
        body = (self.repo / "skill" / "close-session.md").read_text()
        self.assertIn("bash subshell", body, "lock 범위 한계 명시 누락")

    def test_korean_slug_conversion_rule(self):
        """MED #5 가드 — 한국어 → kebab 변환 deterministic rule 명시."""
        body = (self.repo / "skill" / "close-session.md").read_text()
        self.assertIn("한국어 → 영어 의미 번역", body, "kebab 변환 rule 누락")

    def test_cs_alias_path_fallback(self):
        """MED #6 가드 — cs.md 가 tilde expansion fail 시 fallback path 명시."""
        body = (self.repo / "skill" / "cs.md").read_text()
        self.assertIn("$HOME", body, "absolute path fallback 누락")

    def test_recall_latency_not_stale(self):
        """LOW #8 가드 — /recall 메트릭이 갱신 (200ms → 40ms)."""
        body = (self.repo / "skill" / "close-session.md").read_text()
        self.assertNotIn("실측 200ms", body, "/recall 200ms 표기 잔존")
        self.assertIn("p50~40ms", body, "/recall 최신 p50 표기 누락")

    def test_type_tiebreaker_rule(self):
        """LOW #9 가드 — 5 카테고리 tiebreaker rule 명시."""
        body = (self.repo / "skill" / "close-session.md").read_text()
        self.assertIn("tiebreaker", body, "type 결정 tiebreaker 누락")

    def test_install_handles_corrupt_skill_dir(self):
        """codex round-6 A 가드 — SKILL.md 부재 시 corrupt install 보존."""
        sh = (self.repo / "install.sh").read_text()
        self.assertIn("SKILL.md 없음", sh, "corrupt install guard 누락")
        self.assertIn("[ ! -f \"$skill_md\" ]", sh, "SKILL.md 부재 검사 누락")

    def test_install_backup_uses_pid_suffix(self):
        """codex round-6 B 가드 — backup 디렉토리에 $$ PID 추가 (parallel collision 차단)."""
        sh = (self.repo / "install.sh").read_text()
        self.assertIn("mv3-skill-conflict-$(date", sh)
        self.assertIn("-$$", sh, "PID suffix 누락 — 같은 초 parallel install collision")

    def test_slug_reuse_existing_before_translation(self):
        """codex round-6 D 가드 — slug 결정 시 기존 메모리 매칭 우선."""
        body = (self.repo / "skill" / "close-session.md").read_text()
        self.assertIn("기존 메모리에 비슷한 주제가 있으면 그 slug 재사용", body,
                      "slug consistency 강화 누락")

    def test_alt_mode_mtime_tracking_specified(self):
        """codex round-6 E 가드 — Edit/Write alt mode 의 mtime tracking spec 명시."""
        body = (self.repo / "skill" / "close-session.md").read_text()
        self.assertIn("INITIAL_MTIME", body, "mtime tracking spec 누락")
        self.assertIn("CURRENT_MTIME", body, "race 검출 비교 spec 누락")

    def test_cs_warns_on_stale_content(self):
        """codex round-6 F 가드 — cs.md 가 stale content 가능성 안내."""
        body = (self.repo / "skill" / "cs.md").read_text()
        self.assertIn("stale", body, "stale content 경고 누락")


class TestUninstallV320(unittest.TestCase):
    """v3.2.0 — Gemma plist + cache 정리 (com.mindvault.gemma-mlx)."""

    REPO_DIR = Path(__file__).resolve().parents[1]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.launch_agents = Path(self.tmp.name) / "LaunchAgents"
        self.launch_agents.mkdir()
        self.cache = Path(self.tmp.name) / "mv3-gemma"
        self.cache.mkdir()
        (self.cache / ".mv3-step").write_text("deps-ok\n")
        self.plist = self.launch_agents / "com.mindvault.gemma-mlx.plist"
        self.plist.write_text("<plist/>")

    def tearDown(self):
        self.tmp.cleanup()

    def test_removes_gemma_plist_and_cache(self):
        env = os.environ.copy()
        env.update({
            "MV3_LAUNCH_AGENTS": str(self.launch_agents),
            "MV3_GEMMA_CACHE": str(self.cache),
            "MV3_UNINSTALL_GEMMA_ONLY": "1",
            "MV3_UNINSTALL_DRY_LAUNCHCTL": "1",
        })
        r = subprocess.run(
            ["bash", str(self.REPO_DIR / "uninstall.sh")],
            capture_output=True, env=env,
        )
        self.assertEqual(r.returncode, 0, msg=r.stderr.decode())
        self.assertFalse(self.plist.exists())
        self.assertFalse(self.cache.exists())


if __name__ == "__main__":
    unittest.main()
