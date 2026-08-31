from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fixtures" / "community-garden"


class CliFixtureTests(unittest.TestCase):
    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", "-m", "insomnia_media_pipeline", *args],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_repository_fixture_preflights_and_dry_run_writes_nothing(self) -> None:
        story = FIXTURE / "story.txt"
        config = FIXTURE / "project.yaml"

        preflight = self.cli("preflight", "--story", str(story), "--config", str(config))
        self.assertEqual(0, preflight.returncode, preflight.stderr)
        self.assertEqual("PASS", json.loads(preflight.stdout)["status"])

        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "dry-run-output"
            dry = self.cli(
                "dry-run", "--story", str(story), "--config", str(config), "--run-dir", str(run_dir)
            )
            self.assertEqual(0, dry.returncode, dry.stderr)
            self.assertEqual("DRY_RUN", json.loads(dry.stdout)["kind"])
            self.assertFalse(run_dir.exists())

    def test_synthetic_cli_run_status_and_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            created = self.cli(
                "synthetic-run",
                "--story", str(FIXTURE / "story.txt"),
                "--config", str(FIXTURE / "project.yaml"),
                "--run-dir", str(run_dir),
            )
            self.assertEqual(0, created.returncode, created.stderr)
            self.assertEqual("PASS", json.loads(created.stdout)["status"])

            status = self.cli("status", "--run-dir", str(run_dir))
            self.assertEqual(0, status.returncode, status.stderr)
            status_data = json.loads(status.stdout)
            self.assertEqual("PASS", status_data["status"])
            self.assertEqual(15, len(status_data["stages"]))

            resumed = self.cli("resume", "--run-dir", str(run_dir))
            self.assertEqual(0, resumed.returncode, resumed.stderr)
            self.assertEqual("PASS", json.loads(resumed.stdout)["status"])


if __name__ == "__main__":
    unittest.main()
