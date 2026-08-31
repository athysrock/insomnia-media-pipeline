from __future__ import annotations

import json
import tempfile
import unittest
import wave
from pathlib import Path

from insomnia_media_pipeline.contracts import STAGE_CONTRACTS
from insomnia_media_pipeline.media import atempo_filter, chatterbox_controls, musicgen_segment_plan
from insomnia_media_pipeline.pipeline import PipelineError, resume_pipeline, run_pipeline, run_stage

from tests.helpers import make_project


class SyntheticEndToEndTests(unittest.TestCase):
    def test_provider_free_run_produces_verified_fake_package_and_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            story, config = make_project(root)
            run_dir = root / "run"

            result = run_pipeline(story, config, run_dir)

            self.assertEqual("PASS", result["status"])
            for contract in STAGE_CONTRACTS:
                self.assertTrue((run_dir / "staging" / "receipts" / f"{contract.name}.json").is_file())
                for relative in contract.outputs:
                    self.assertTrue((run_dir / relative).is_file(), relative)

            with wave.open(str(run_dir / "audio" / "narration.wav"), "rb") as narration:
                self.assertGreater(narration.getnframes(), 0)
            with wave.open(str(run_dir / "audio" / "mix.wav"), "rb") as mix:
                self.assertGreater(mix.getnframes(), 0)

            checkpoint = json.loads((run_dir / "staging" / "checkpoints" / "images.json").read_text())
            self.assertEqual("COMPLETED", checkpoint["status"])
            self.assertEqual(checkpoint["expected"], checkpoint["completed"])

            audit = json.loads((run_dir / "audit" / "audit.json").read_text())
            package = json.loads((run_dir / "package.json").read_text())
            self.assertEqual("PASS", audit["status"])
            self.assertTrue(audit["synthetic"])
            self.assertEqual("PASS", package["status"])
            self.assertEqual("synthetic-fake", package["media_kind"])
            self.assertIn("not a production video", package["notice"])
            self.assertTrue((run_dir / "video" / "video.mp4").read_bytes().startswith(b"SYNTHETIC-MEDIA\n"))

            resumed = resume_pipeline(run_dir)
            self.assertEqual("PASS", resumed["status"])
            self.assertTrue(all(item["status"] == "REUSED" for item in resumed["stages"]))

    def test_audit_binding_rejects_media_changed_after_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            story, config = make_project(root)
            run_dir = root / "run"
            run_pipeline(story, config, run_dir)
            video = run_dir / "video" / "video.mp4"
            video.write_bytes(video.read_bytes() + b"changed")

            with self.assertRaisesRegex(PipelineError, "audit fingerprint"):
                run_stage(run_dir, "finalize")

    def test_native_control_surface_and_continuation_plan_are_truthful(self) -> None:
        controls = chatterbox_controls(exaggeration=0.4, cfg_weight=0.55, temperature=0.75)
        self.assertEqual({"exaggeration", "cfg_weight", "temperature"}, set(controls))
        self.assertEqual("atempo=2,atempo=2", atempo_filter(4.0))
        self.assertEqual("atempo=0.5,atempo=0.5", atempo_filter(0.25))
        self.assertEqual([30, 25, 15], musicgen_segment_plan(70, 30, 5))


if __name__ == "__main__":
    unittest.main()
