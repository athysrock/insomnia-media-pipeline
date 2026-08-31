from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from insomnia_media_pipeline.pipeline import (
    PipelineError,
    dry_run,
    initialize_run,
    run_stage,
)
from insomnia_media_pipeline.config import load_project_config
from insomnia_media_pipeline.media import ffmpeg_assemble_video
from insomnia_media_pipeline.stages import _first_wav_signal, _png, _scene_assignments, _wav_duration

from tests.helpers import make_project


class ArtifactPipelineTests(unittest.TestCase):
    def test_omitted_voice_delivery_uses_soothing_project_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, config_path = make_project(root)
            data = json.loads(config_path.read_text(encoding="utf-8"))
            data["voice"] = {"reference_audio": data["voice"]["reference_audio"]}
            config_path.write_text(json.dumps(data), encoding="utf-8")

            voice = load_project_config(config_path).voice

        self.assertEqual(0.5, voice.exaggeration)
        self.assertEqual(0.5, voice.cfg_weight)
        self.assertEqual(0.8, voice.temperature)
        self.assertEqual(0.9, voice.tempo)

    def test_first_caption_burns_at_speech_onset_and_video_tracks_audio_duration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "scene.png"
            image.write_bytes(_png(320, 180, (80, 120, 160)))
            audio = root / "audio.wav"
            with wave.open(str(audio), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16_000)
                output.writeframes(b"\x00\x00" * 3_200 + b"\xff\x1f" * 28_800)
            captions = root / "captions.srt"
            captions.write_text(
                "1\n00:00:00,200 --> 00:00:01,900\nCaption starts with speech.\n",
                encoding="utf-8",
            )
            video = root / "video.mp4"

            self.assertAlmostEqual(0.2, _first_wav_signal(audio), places=3)

            ffmpeg_assemble_video(
                [{"absolute_image": str(image), "duration_seconds": 9.0}],
                audio,
                captions,
                video,
                width=320,
                height=180,
                fps=24,
                crf=18,
                content_duration_seconds=2.15,
            )

            duration = float(
                subprocess.run(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout
            )
            before = subprocess.run(
                ["ffmpeg", "-v", "error", "-i", str(video), "-vf", "select=eq(n\\,2),crop=320:60:0:120,format=gray", "-frames:v", "1", "-f", "rawvideo", "-"],
                check=True,
                capture_output=True,
            ).stdout
            at_onset = subprocess.run(
                ["ffmpeg", "-v", "error", "-i", str(video), "-vf", "select=eq(n\\,6),crop=320:60:0:120,format=gray", "-frames:v", "1", "-f", "rawvideo", "-"],
                check=True,
                capture_output=True,
            ).stdout

        self.assertGreaterEqual(duration, 2.1)
        self.assertLessEqual(duration - 2.0, 1.0)
        before_mean = sum(before) / len(before)
        onset_mean = sum(at_onset) / len(at_onset)
        self.assertGreater(abs(onset_mean - before_mean), 0.5)

    def test_scene_assignments_cover_every_pacing_chunk(self) -> None:
        chunks = [
            {"index": index, "duration_seconds": float(index), "pause_after_seconds": 0.5}
            for index in range(1, 6)
        ]

        assignments = _scene_assignments(chunks, 2)

        self.assertEqual([1, 3], [item["source_chunk"] for item in assignments])
        self.assertEqual([4.0, 13.5], [item["duration_seconds"] for item in assignments])
        self.assertEqual(17.5, sum(item["duration_seconds"] for item in assignments))

    def test_wav_duration_falls_back_to_ffprobe_for_extensible_wav(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audio.wav"
            with wave.open(str(path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16_000)
                output.writeframes(b"\x00\x00" * 8_000)

            with patch("insomnia_media_pipeline.stages.wave.open", side_effect=wave.Error("unknown format: 65534")):
                duration = _wav_duration(path)

        self.assertAlmostEqual(0.5, duration, places=3)

    def test_dry_run_validates_and_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            story, config = make_project(root)
            run_dir = root / "run"

            report = dry_run(story, config, run_dir)

            self.assertEqual("PASS", report["status"])
            self.assertEqual("DRY_RUN", report["kind"])
            self.assertIn("music_brief", report["stages"])
            self.assertFalse(run_dir.exists())

    def test_run_uses_artifacts_and_never_snapshots_or_hashes_live_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            story, config_path = make_project(root)
            run_dir = root / "run"

            initialize_run(story, config_path, run_dir)

            manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
            self.assertEqual("input/story.txt", manifest["story_artifact"])
            self.assertEqual(str(config_path.resolve()), manifest["config_path"])
            self.assertFalse((run_dir / "project.json").exists())
            self.assertFalse((run_dir / "prompts").exists())
            self.assertNotIn("config_hash", json.dumps(manifest))
            self.assertNotIn("prompt_hash", json.dumps(manifest))

            first = run_stage(run_dir, "authoring")
            second = run_stage(run_dir, "authoring")
            self.assertEqual("COMPLETED", first["status"])
            self.assertEqual("REUSED", second["status"])

            run_stage(run_dir, "pacing")
            run_stage(run_dir, "scene_prompts")

            config_data = json.loads(config_path.read_text(encoding="utf-8"))
            config_data["music"]["direction"] = "Playful hand percussion rising into a sunny finale"
            config_path.write_text(json.dumps(config_data), encoding="utf-8")
            result = run_stage(run_dir, "music_brief")

            brief = json.loads((run_dir / "music" / "brief.json").read_text(encoding="utf-8"))
            self.assertEqual("COMPLETED", result["status"])
            self.assertIn("sunny finale", brief["brief"])
            self.assertTrue({"neighbors", "garden"} & set(brief["story_terms"]))
            receipt_text = (run_dir / "staging" / "receipts" / "music_brief.json").read_text(encoding="utf-8")
            self.assertNotIn("config_hash", receipt_text)
            self.assertNotIn("prompt_hash", receipt_text)

    def test_changed_artifact_invalidates_stage_but_changed_prompt_does_not(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            story, config_path = make_project(root)
            run_dir = root / "run"
            initialize_run(story, config_path, run_dir)
            run_stage(run_dir, "authoring")
            receipt_path = run_dir / "staging" / "receipts" / "authoring.json"
            first_receipt = receipt_path.read_bytes()

            (root / "prompts" / "authoring.txt").write_text("Revised policy: {story}\n", encoding="utf-8")
            self.assertEqual("REUSED", run_stage(run_dir, "authoring")["status"])
            self.assertEqual(first_receipt, receipt_path.read_bytes())

            (run_dir / "input" / "story.txt").write_text(
                "Friends repaired a bicycle and rode together through the park.\n",
                encoding="utf-8",
            )
            self.assertEqual("COMPLETED", run_stage(run_dir, "authoring")["status"])
            self.assertIn("bicycle", (run_dir / "authored" / "story.txt").read_text(encoding="utf-8"))

    def test_missing_dependency_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            story, config = make_project(root)
            run_dir = root / "run"
            initialize_run(story, config, run_dir)

            with self.assertRaisesRegex(PipelineError, "requires artifact"):
                run_stage(run_dir, "pacing")


if __name__ == "__main__":
    unittest.main()
