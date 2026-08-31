from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from insomnia_media_pipeline.config import ConfigError, load_project_config
from insomnia_media_pipeline.deadlines import (
    MAX_COMFYUI_TIMEOUT_SECONDS,
    MAX_LLM_TIMEOUT_SECONDS,
    real_stage_budgets,
)
from insomnia_media_pipeline.pipeline import dry_run
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

    def test_music_overlap_must_be_positive_and_smaller_than_segment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, config_path = self.make_project(root)
            data = valid_config()
            data["music"]["overlap"] = 0
            config_path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(ConfigError, "music.overlap"):
                load_project_config(config_path)

            data["music"]["overlap"] = 1
            config_path.write_text(json.dumps(data), encoding="utf-8")
            self.assertEqual(1, load_project_config(config_path).music.overlap)

    def test_only_implemented_voice_controls_are_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, config_path = self.make_project(root)
            data = valid_config()
            data["voice"]["emotion"] = "excited"
            config_path.write_text(json.dumps(data), encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "unknown key.*emotion"):
                load_project_config(config_path)

    def test_real_mode_preflight_and_dry_run_reject_incomplete_provider_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            story, config_path = self.make_project(root)
            data = valid_config()
            data["runtime"]["mode"] = "real"
            config_path.write_text(json.dumps(data), encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "llm.command"):
                run_preflight(story, config_path)
            with self.assertRaisesRegex(ConfigError, "llm.command"):
                dry_run(story, config_path, root / "run")
            self.assertFalse((root / "run").exists())

    def test_real_mode_preflight_requires_comfyui_url_and_workflow_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            story, config_path = self.make_project(root)
            data = valid_config()
            data["runtime"]["mode"] = "real"
            data["llm"] = {"command": "local-llm"}
            config_path.write_text(json.dumps(data), encoding="utf-8")

            with self.assertRaisesRegex(ConfigError, "comfyui.api_url"):
                run_preflight(story, config_path)

    def test_real_mode_preflight_checks_provider_urls_paths_and_timeouts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            story, config_path = self.make_project(root)
            workflow = root / "workflow.json"
            workflow.write_text(json.dumps({"node": {"inputs": {"text": "{{PROMPT}}"}}}), encoding="utf-8")
            base = valid_config()
            base["runtime"]["mode"] = "real"
            base["runtime"]["retries"] = 0
            base["llm"] = {"command": "local-llm", "timeout_seconds": 900}
            base["comfyui"] = {
                "api_url": "http://127.0.0.1:8188",
                "workflow_template": "workflow.json",
                "timeout_seconds": 300,
            }

            invalid_cases = (
                ("llm timeout", lambda data: data["llm"].update(timeout_seconds=0), "llm.timeout_seconds"),
                ("ComfyUI URL", lambda data: data["comfyui"].update(api_url="file:///tmp/comfy"), "comfyui.api_url"),
                ("ComfyUI timeout", lambda data: data["comfyui"].update(timeout_seconds=0), "comfyui.timeout_seconds"),
                ("workflow path", lambda data: data["comfyui"].update(workflow_template="missing.json"), "workflow_template"),
            )
            for label, mutate, error in invalid_cases:
                with self.subTest(label=label):
                    data = json.loads(json.dumps(base))
                    mutate(data)
                    config_path.write_text(json.dumps(data), encoding="utf-8")
                    with self.assertRaisesRegex(ConfigError, error):
                        run_preflight(story, config_path)

            config_path.write_text(json.dumps(base), encoding="utf-8")
            self.assertEqual("PASS", run_preflight(story, config_path)["status"])

    def test_real_mode_rejects_retries_and_provider_timeouts_beyond_durable_budgets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            story, config_path = self.make_project(root)
            (root / "workflow.json").write_text(json.dumps({"node": {"inputs": {}}}), encoding="utf-8")
            base = valid_config()
            base["runtime"].update(mode="real", retries=0)
            base["llm"] = {"command": "local-llm", "timeout_seconds": MAX_LLM_TIMEOUT_SECONDS}
            base["comfyui"] = {
                "api_url": "http://127.0.0.1:8188",
                "workflow_template": "workflow.json",
                "timeout_seconds": MAX_COMFYUI_TIMEOUT_SECONDS,
            }

            config_path.write_text(json.dumps(base), encoding="utf-8")
            self.assertEqual("PASS", run_preflight(story, config_path)["status"])
            for budget in real_stage_budgets(
                llm_timeout=MAX_LLM_TIMEOUT_SECONDS,
                comfyui_timeout=MAX_COMFYUI_TIMEOUT_SECONDS,
            ).values():
                self.assertLessEqual(budget.worst_case_seconds, budget.deadline_seconds)

            for label, mutate, error in (
                ("retries", lambda data: data["runtime"].update(retries=2), "runtime.retries must be 0"),
                ("huge LLM timeout", lambda data: data["llm"].update(timeout_seconds=999999), "llm.timeout_seconds"),
                ("huge image timeout", lambda data: data["comfyui"].update(timeout_seconds=999999), "comfyui.timeout_seconds"),
            ):
                with self.subTest(label=label):
                    data = json.loads(json.dumps(base))
                    mutate(data)
                    config_path.write_text(json.dumps(data), encoding="utf-8")
                    with self.assertRaisesRegex(ConfigError, error):
                        run_preflight(story, config_path)

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
