"""v3.2.0 — Zero-Touch Install 검증.

install.sh 안의 핵심 헬퍼 (do_step / print_next_step / Apple Silicon guard) 와
checkpoint resume 동작을 격리 테스트.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_DIR / "install.sh"


class TestAppleSiliconGuard(unittest.TestCase):
    """install.sh 가 non-arm64 환경에서 경고 + 분기하는지."""

    def test_non_arm64_prints_warning(self):
        """ARCH_OVERRIDE=x86_64 환경변수로 가드 강제 트리거."""
        env = os.environ.copy()
        env["ARCH_OVERRIDE"] = "x86_64"
        env["MV3_GUARD_ONLY"] = "1"
        r = subprocess.run(
            ["bash", str(INSTALL_SH)],
            capture_output=True,
            env=env,
            input=b"n\n",
        )
        self.assertIn(b"Apple Silicon", r.stdout + r.stderr)
        self.assertIn(b"v3.3.0", r.stdout + r.stderr)
        self.assertNotEqual(r.returncode, 0)

    def test_arm64_skips_warning(self):
        """ARCH_OVERRIDE=arm64 면 가드 통과."""
        env = os.environ.copy()
        env["ARCH_OVERRIDE"] = "arm64"
        env["MV3_GUARD_ONLY"] = "1"
        r = subprocess.run(
            ["bash", str(INSTALL_SH)],
            capture_output=True,
            env=env,
        )
        self.assertNotIn(b"Apple Silicon", r.stdout + r.stderr)
        self.assertEqual(r.returncode, 0)


class TestDoStep(unittest.TestCase):
    """do_step 헬퍼의 idempotent + resume 동작."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.step_file = Path(self.tmp.name) / ".mv3-step"

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, snippet: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", "-c",
             f"export MV3_SOURCE_HELPERS_ONLY=1 ARCH_OVERRIDE=arm64; "
             f"source {INSTALL_SH}; {snippet}"],
            capture_output=True,
        )

    def test_do_step_records_on_success(self):
        r = self._run(f'do_step myname {self.step_file} "true"')
        self.assertEqual(r.returncode, 0)
        self.assertIn("myname\n", self.step_file.read_text())

    def test_do_step_skips_if_already_done(self):
        self.step_file.write_text("myname\n")
        r = self._run(f'do_step myname {self.step_file} "echo SHOULD_NOT_RUN"')
        self.assertEqual(r.returncode, 0)
        self.assertNotIn(b"SHOULD_NOT_RUN", r.stdout)
        self.assertIn(b"already done", r.stdout)

    def test_do_step_no_record_on_failure(self):
        r = self._run(f'do_step myname {self.step_file} "false"')
        self.assertNotEqual(r.returncode, 0)
        self.assertFalse(self.step_file.exists() and "myname" in self.step_file.read_text())

    def test_print_next_step_known_step(self):
        r = self._run('print_next_step downloaded')
        self.assertIn(b"Next:", r.stdout)
        self.assertIn(b"huggingface_hub", r.stdout)


if __name__ == "__main__":
    unittest.main()
