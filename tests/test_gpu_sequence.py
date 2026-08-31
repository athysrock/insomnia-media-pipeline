from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SEQUENCE = ROOT / "tools" / "run_8gb_sequence.sh"


class GpuSequenceTests(unittest.TestCase):
    def fake_python(self, root: Path) -> Path:
        executable = root / "fake-python"
        executable.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" >> \"$TEST_LOG\"\n"
            "case \"$*\" in\n"
            "  *'conductor.worker'*'--exclude-task'*)\n"
            "    echo $$ > \"$GENERAL_PID_FILE\"\n"
            "    [ \"${GENERAL_FAIL:-0}\" = 0 ] || exit 12\n"
            "    trap 'exit 0' TERM INT\n"
            "    while :; do sleep 1; done\n"
            "    ;;\n"
            "  *'conductor.client launch'*) printf '%s\\n' '{\"workflow_id\":\"workflow-123\"}' ;;\n"
            "  *'conductor.client monitor'*)\n"
            "    if [ \"${EARLY_FAIL:-0}\" = 1 ]; then\n"
            "      printf '%s\\n' '{\"status\":\"FAILED\",\"tasks\":[]}'\n"
            "    elif [ -f \"$MUSIC_MARKER\" ]; then\n"
            "      printf '%s\\n' '{\"status\":\"COMPLETED\",\"tasks\":[]}'\n"
            "    elif [ -f \"$RUN_DIR/staging/receipts/tts.json\" ]; then\n"
            "      : > \"$MUSIC_SCHEDULED_MARKER\"\n"
            "      printf '%s\\n' '{\"status\":\"RUNNING\",\"tasks\":[{\"taskDefName\":\"insomnia_media_pipeline_music\",\"status\":\"SCHEDULED\"}]}'\n"
            "    else\n"
            "      printf '%s\\n' '{\"status\":\"RUNNING\",\"tasks\":[{\"taskDefName\":\"insomnia_media_pipeline_tts\",\"status\":\"SCHEDULED\"}]}'\n"
            "    fi\n"
            "    ;;\n"
            "  *'insomnia_media_pipeline_tts'*'--once'*)\n"
            "    [ \"${TTS_FAIL:-0}\" = 0 ] || exit 7\n"
            "    mkdir -p \"$RUN_DIR/staging/receipts\"\n"
            "    : > \"$RUN_DIR/staging/receipts/tts.json\"\n"
            "    ;;\n"
            "  *'insomnia_media_pipeline_music'*'--once'*)\n"
            "    test -f \"$RUN_DIR/staging/receipts/tts.json\" || exit 8\n"
            "    test -f \"$MUSIC_SCHEDULED_MARKER\" || exit 10\n"
            "    [ \"${MUSIC_IDLE:-0}\" = 0 ] || exit 0\n"
            "    : > \"$RUN_DIR/staging/receipts/music.json\"\n"
            "    : > \"$MUSIC_MARKER\"\n"
            "    ;;\n"
            "  *) exit 9 ;;\n"
            "esac\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
        return executable

    def run_sequence(
        self,
        root: Path,
        *,
        tts_fail: bool = False,
        early_fail: bool = False,
        music_idle: bool = False,
        general_fail: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        fake = self.fake_python(root)
        story = root / "story.txt"
        config = root / "project.yaml"
        story.write_text("story\n", encoding="utf-8")
        config.write_text("config\n", encoding="utf-8")
        environment = os.environ.copy()
        environment.update(
            {
                "PYTHON": str(fake),
                "TTS_PYTHON": str(fake),
                "MUSICGEN_PYTHON": str(fake),
                "TEST_LOG": str(root / "calls.log"),
                "GENERAL_PID_FILE": str(root / "general.pid"),
                "MUSIC_MARKER": str(root / "music.done"),
                "MUSIC_SCHEDULED_MARKER": str(root / "music.scheduled"),
                "POLL_INTERVAL": "0.01",
                "TTS_FAIL": "1" if tts_fail else "0",
                "EARLY_FAIL": "1" if early_fail else "0",
                "MUSIC_IDLE": "1" if music_idle else "0",
                "GENERAL_FAIL": "1" if general_fail else "0",
            }
        )
        return subprocess.run(
            [str(SEQUENCE), str(story), str(config), str(root / "run")],
            cwd=ROOT,
            env=environment,
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )

    def test_sequence_orders_one_shot_gpu_workers_and_stops_general_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = self.run_sequence(root)

            self.assertEqual(0, result.returncode, result.stderr)
            calls = (root / "calls.log").read_text(encoding="utf-8").splitlines()
            tts = next(index for index, call in enumerate(calls) if "insomnia_media_pipeline_tts" in call and "--once" in call)
            music = next(index for index, call in enumerate(calls) if "insomnia_media_pipeline_music" in call and "--once" in call)
            general = [
                index
                for index, call in enumerate(calls)
                if "conductor.worker" in call and "--exclude-task" in call
            ]
            self.assertEqual(3, len(general))
            self.assertLess(general[0], tts)
            self.assertLess(tts, general[1])
            self.assertLess(general[1], music)
            self.assertLess(music, general[2])
            self.assertTrue((root / "run" / "staging" / "receipts" / "tts.json").is_file())
            pid = int((root / "general.pid").read_text(encoding="utf-8"))
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def test_sequence_fails_closed_and_cleans_up_when_tts_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = self.run_sequence(root, tts_fail=True)

            self.assertNotEqual(0, result.returncode)
            calls = (root / "calls.log").read_text(encoding="utf-8")
            self.assertNotIn("insomnia_media_pipeline_music --once", calls)
            pid = int((root / "general.pid").read_text(encoding="utf-8"))
            for _ in range(20):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.01)
            else:
                self.fail("general worker survived fail-closed cleanup")

    def test_sequence_fails_if_general_worker_exits_while_workflow_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = self.run_sequence(root, general_fail=True)

            self.assertNotEqual(0, result.returncode)
            calls = (root / "calls.log").read_text(encoding="utf-8")
            self.assertNotIn("insomnia_media_pipeline_tts --once", calls)

    def test_sequence_fails_if_one_shot_music_worker_claims_no_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = self.run_sequence(root, music_idle=True)

            self.assertNotEqual(0, result.returncode)
            self.assertFalse((root / "music.done").exists())

    def test_sequence_stops_if_workflow_fails_before_tts_is_scheduled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = self.run_sequence(root, early_fail=True)

            self.assertNotEqual(0, result.returncode)
            calls = (root / "calls.log").read_text(encoding="utf-8")
            self.assertNotIn("insomnia_media_pipeline_tts --once", calls)
            self.assertNotIn("insomnia_media_pipeline_music --once", calls)


if __name__ == "__main__":
    unittest.main()
