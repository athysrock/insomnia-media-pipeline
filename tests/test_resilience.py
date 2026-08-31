from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from insomnia_media_pipeline.pipeline import run_pipeline, run_stage, run_status
from insomnia_media_pipeline.stages import normalize_srt

from tests.helpers import make_project


class ResilienceTests(unittest.TestCase):
    def test_missing_checkpointed_image_is_regenerated_and_status_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            story, config = make_project(root)
            run_dir = root / "run"
            run_pipeline(story, config, run_dir)
            index = json.loads((run_dir / "images" / "index.json").read_text(encoding="utf-8"))
            missing = run_dir / index["images"][0]["path"]
            missing.unlink()

            self.assertEqual("INCOMPLETE", run_status(run_dir)["status"])
            result = run_stage(run_dir, "images")
            self.assertEqual("COMPLETED", result["status"])
            self.assertTrue(missing.is_file())
            self.assertEqual("PASS", run_status(run_dir)["status"])

    def test_caption_postprocess_is_idempotent(self) -> None:
        source = "1\n00:00:00,000 --> 00:00:01,000\nA bright morning.\n\n"
        once = normalize_srt(source)
        self.assertEqual(once, normalize_srt(once))


if __name__ == "__main__":
    unittest.main()
