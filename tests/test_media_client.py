from __future__ import annotations

import unittest
from unittest.mock import patch

from insomnia_media_pipeline.media import ComfyUIClient, MediaError


class ComfyUIClientTests(unittest.TestCase):
    def test_client_rejects_non_http_api_urls(self) -> None:
        with self.assertRaisesRegex(MediaError, "http or https"):
            ComfyUIClient("file:///tmp/provider")

    def test_start_timeout_terminates_process_started_by_client(self) -> None:
        class Process:
            def __init__(self) -> None:
                self.terminated = False
                self.waited = False

            def terminate(self) -> None:
                self.terminated = True

            def wait(self, timeout: int) -> None:
                self.waited = True

        process = Process()
        client = ComfyUIClient("http://127.0.0.1:8188", start_command="comfyui", timeout=0)
        with patch.object(client, "healthy", return_value=False), patch(
            "insomnia_media_pipeline.media.subprocess.Popen", return_value=process
        ):
            with self.assertRaisesRegex(MediaError, "did not become healthy"):
                client.start()

        self.assertTrue(process.terminated)
        self.assertTrue(process.waited)
        self.assertFalse(client.started_here)

    def test_stop_does_not_touch_an_already_running_external_instance(self) -> None:
        client = ComfyUIClient("http://127.0.0.1:8188", stop_command="stop-comfyui")
        with patch.object(client, "healthy", return_value=True), patch(
            "insomnia_media_pipeline.media._run"
        ) as run:
            client.start()
            client.stop()
        run.assert_not_called()

    def test_render_workflow_preserves_arbitrary_prompt_text(self) -> None:
        prompt = 'first line\nsecond\tline with "quotes" and \\slashes'
        prefix = 'scene/one "draft"'
        template = {
            "prompt": {"inputs": {"text": "{{PROMPT}}", "filename_prefix": "{{OUTPUT_PREFIX}}"}},
            "combined": "Illustration: {{PROMPT}} / {{OUTPUT_PREFIX}}",
            "unchanged": [1, None, True],
        }

        rendered = ComfyUIClient.render_workflow(template, prompt, prefix)

        self.assertEqual(prompt, rendered["prompt"]["inputs"]["text"])
        self.assertEqual(prefix, rendered["prompt"]["inputs"]["filename_prefix"])
        self.assertEqual(f"Illustration: {prompt} / {prefix}", rendered["combined"])
        self.assertEqual([1, None, True], rendered["unchanged"])
        self.assertEqual("{{PROMPT}}", template["prompt"]["inputs"]["text"])


if __name__ == "__main__":
    unittest.main()
