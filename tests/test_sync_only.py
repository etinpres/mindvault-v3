"""v3.x — 배포 후크화 (MV3_SYNC_ONLY 경량 배포 + deploy drift 백스톱) 검증.

배경: 프롬프트 보정 커밋이 repo 에는 있었으나 배포 경로엔 3일간 미반영이라
contradiction 오탐이 발생(2026-05-28→06-01). 재발 방지로 (1) 커밋 시 자동 배포,
(2) 세션 시작 drift 경고를 추가했다. 이 테스트는 두 메커니즘의 핵심 불변식을 건다.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_DIR / "install.sh"
DRIFT_SCRIPT = REPO_DIR / "scripts" / "deploy_drift_check.py"

# SYNC 모드에서 절대 나오면 안 되는 슬로우 단계 마커.
SLOW_MARKERS = [
    "Installing Python dependencies",
    "Waiting for Arctic-ko",
    "Building FTS5",
    "Pre-warming Gemma",
]


class TestSyncOnlyDeploy(unittest.TestCase):
    """MV3_SYNC_ONLY=1 ./install.sh 가 파일만 빠르게 배포하고 슬로우 단계는 skip."""

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.env = os.environ.copy()
        self.env["HOME"] = self.home.name
        self.env["ARCH_OVERRIDE"] = "arm64"
        self.env["MV3_SYNC_ONLY"] = "1"
        self.env["MV3_SKIP_GIT_WIRE"] = "1"  # 실제 repo git config 비변경
        # 런타임 디렉토리를 temp HOME 하위로 강제(실서비스 데이터 격리).
        self.env["MV3_RUNTIME_DIR"] = str(Path(self.home.name) / ".claude" / "mindvault-v3")

    def tearDown(self):
        self.home.cleanup()

    def _run(self):
        return subprocess.run(
            ["bash", str(INSTALL_SH)],
            capture_output=True, env=self.env, timeout=120,
        )

    def test_deploys_runtime_code(self):
        """contradiction_detector.py 가 배포되고 repo src 와 바이트 일치."""
        r = self._run()
        self.assertEqual(r.returncode, 0, msg=r.stdout.decode() + r.stderr.decode())
        deployed = Path(self.home.name) / ".claude" / "scripts" / "mindvault" / "contradiction_detector.py"
        self.assertTrue(deployed.exists(), "detector 미배포")
        self.assertEqual(
            deployed.read_bytes(),
            (REPO_DIR / "src" / "contradiction_detector.py").read_bytes(),
            "배포본 != repo src (sync 불완전)",
        )

    def test_deploys_drift_hook(self):
        """deploy_drift_check.py 가 배포되고 SessionStart 에 등록됨."""
        r = self._run()
        self.assertEqual(r.returncode, 0, msg=r.stdout.decode() + r.stderr.decode())
        hook = Path(self.home.name) / ".claude" / "scripts" / "mindvault" / "deploy_drift_check.py"
        self.assertTrue(hook.exists(), "drift 훅 미배포")
        settings = json.loads((Path(self.home.name) / ".claude" / "settings.json").read_text())
        cmds = [
            h.get("command", "")
            for entry in settings["hooks"].get("SessionStart", [])
            for h in entry.get("hooks", [])
        ]
        self.assertTrue(
            any("deploy_drift_check.py" in c for c in cmds),
            f"SessionStart 미등록: {cmds}",
        )

    def test_skips_slow_steps(self):
        """모델/pip/헬스체크/인덱싱 등 슬로우 단계 마커가 출력에 없어야."""
        r = self._run()
        out = (r.stdout + r.stderr).decode()
        for marker in SLOW_MARKERS:
            self.assertNotIn(marker, out, f"SYNC 모드에서 슬로우 단계 실행됨: {marker}")


class TestDeployDriftCheck(unittest.TestCase):
    """deploy_drift_check.py: 일치=무출력 / 불일치=경고 JSON."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / "repo"
        self.home = root / "home"
        self.runtime = self.home / ".claude" / "mindvault-v3"
        self.src = self.repo / "src"
        self.deployed = self.home / ".claude" / "scripts" / "mindvault"
        self.src.mkdir(parents=True)
        self.deployed.mkdir(parents=True)
        self.runtime.mkdir(parents=True)
        (self.runtime / ".repo-path").write_text(str(self.repo))
        # 동일 내용 한 쌍 배치.
        (self.src / "foo.py").write_text("print('v1')\n")
        (self.deployed / "foo.py").write_text("print('v1')\n")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self):
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        env["MV3_RUNTIME_DIR"] = str(self.runtime)
        return subprocess.run(
            [sys.executable, str(DRIFT_SCRIPT)],
            capture_output=True, env=env, timeout=30, text=True,
        )

    def test_no_drift_silent(self):
        r = self._run()
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "", "일치 시 출력이 있으면 안 됨")

    def test_drift_warns(self):
        # repo src 를 앞서게 수정 → 배포본과 불일치.
        (self.src / "foo.py").write_text("print('v2 fixed')\n")
        r = self._run()
        self.assertEqual(r.returncode, 0)
        payload = json.loads(r.stdout)
        ctx = payload["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(payload["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn("foo.py", ctx)
        self.assertIn("drift", ctx)

    def test_no_repo_path_silent(self):
        """.repo-path 부재(엔드유저 설치) 시 조용히 skip."""
        (self.runtime / ".repo-path").unlink()
        r = self._run()
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
