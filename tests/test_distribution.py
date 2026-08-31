from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DistributionTests(unittest.TestCase):
    def test_wheel_installs_pipeline_and_conductor_clis_outside_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wheelhouse = root / "wheelhouse"
            target = root / "site"
            outside = root / "outside"
            wheelhouse.mkdir()
            outside.mkdir()

            built = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    "--no-deps",
                    "--no-build-isolation",
                    "--wheel-dir",
                    str(wheelhouse),
                    str(ROOT),
                ],
                cwd=outside,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, built.returncode, built.stderr)
            wheel = next(wheelhouse.glob("*.whl"))
            installed = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--no-deps", "--target", str(target), str(wheel)],
                cwd=outside,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(0, installed.returncode, installed.stderr)

            self.assertTrue((target / "conductor" / "definitions" / "task-definitions.json").is_file())
            self.assertTrue(
                (target / "conductor" / "definitions" / "workflows" / "insomnia_media_pipeline_v1.json").is_file()
            )

            env = os.environ.copy()
            env["PYTHONPATH"] = str(target)
            for module in ("insomnia_media_pipeline", "conductor.client", "conductor.worker"):
                result = subprocess.run(
                    [sys.executable, "-m", module, "--help"],
                    cwd=outside,
                    env=env,
                    text=True,
                    capture_output=True,
                    check=False,
                )
                self.assertEqual(0, result.returncode, f"{module}: {result.stderr}")


if __name__ == "__main__":
    unittest.main()
