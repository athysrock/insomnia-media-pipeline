from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from insomnia_media_pipeline.config import ConfigError, load_project_config
from insomnia_media_pipeline.preflight import run_preflight
from insomnia_media_pipeline.prompts import read_prompt

from tests.helpers import PROMPT_NAMES, REQUIRED_FIELD, valid_config, write_voice


class PreflightTests(unittest.TestCase):
    def make_project(self, root: Path) -> tuple[Path, Path]:
        story = root / "story.txt"
        story.write_text("Neighbors build a cheerful garden together.\n", encoding="utf-8")
        write_voice(root / "voice.wav")
        prompt_dir = root / "prompts"
        prompt_dir.mkdir()
        for name in PROMPT_NAMES:
            field = REQUIRED_FIELD[name]
            text = f"{name} policy v1: {{{field}}}\n"
            if name == "music_brief":
                text += "Story: {story}\n"
            (prompt_dir / f"{name}.txt").write_text(text, encoding="utf-8")
        config_path = root / "project.json"
        config_path.write_text(json.dumps(valid_config()), encoding="utf-8")
        return story, config_path

    def test_json_preflight_resolves_voice_and_prompts_from_config_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            story, config_path = self.make_project(root)

            report = run_preflight(story, config_path)

            self.assertEqual("PASS", report["status"])
            self.assertEqual((root / "voice.wav").resolve(), report["voice_path"])
            self.assertAlmostEqual(0.5, report["voice_duration_seconds"], places=2)
            self.assertEqual(set(PROMPT_NAMES), set(report["prompts_checked"]))

    def test_yaml_config_is_supported_without_snapshot_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, config_path = self.make_project(root)
            yaml_path = root / "project.yaml"
            yaml_path.write_text(
                "project:\n"
                "  name: Neighborhood Field Day\n"
                "voice:\n"
                "  reference_audio: voice.wav\n"
                "  exaggeration: 0.45\n"
                "  cfg_weight: 0.5\n"
                "  temperature: 0.8\n"
                "  tempo: 1.05\n"
                "music:\n"
                "  direction: Warm acoustic motion with a bright finish\n"
                "  model: facebook/musicgen-small\n"
                "  segment_duration: 30\n"
                "  overlap: 5\n"
                "prompts:\n"
                "  directory: prompts\n"
                "runtime:\n"
                "  mode: synthetic\n"
                "  retries: 2\n",
                encoding="utf-8",
            )

            config = load_project_config(yaml_path)

            self.assertEqual("Neighborhood Field Day", config.project_name)
            self.assertEqual((root / "voice.wav").resolve(), config.voice.reference_audio)

    def test_voice_reference_is_required_and_has_no_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, config_path = self.make_project(root)
            data = valid_config()
            del data["voice"]["reference_audio"]
            config_path.write_text(json.dumps(data), encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "voice.reference_audio is required"):
                load_project_config(config_path)

    def test_preflight_rejects_missing_or_non_wav_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            story, config_path = self.make_project(root)
            (root / "voice.wav").unlink()
            with self.assertRaisesRegex(ConfigError, "does not exist"):
                run_preflight(story, config_path)

            (root / "voice.wav").write_text("not audio", encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "valid PCM WAV"):
                run_preflight(story, config_path)

    def test_only_implemented_voice_controls_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, config_path = self.make_project(root)
            data = valid_config()
            data["voice"]["emotion"] = "excited"
            config_path.write_text(json.dumps(data), encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "unknown key.*emotion"):
                load_project_config(config_path)

    def test_prompt_is_a_strict_live_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, config_path = self.make_project(root)
            config = load_project_config(config_path)
            prompt_path = root / "prompts" / "pacing.txt"

            self.assertEqual("pacing policy v1: {story}\n", read_prompt(config, "pacing"))
            prompt_path.write_text("pacing policy v2: {story}\n", encoding="utf-8")
            self.assertEqual("pacing policy v2: {story}\n", read_prompt(config, "pacing"))


if __name__ == "__main__":
    unittest.main()
