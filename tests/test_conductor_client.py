from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from conductor.client import get_workflow, normalize_api_url, register_definitions, start_workflow
from conductor.worker import TASK_TYPES, poll_once, select_task_types

from tests.helpers import make_project


ROOT = Path(__file__).resolve().parents[1]


class RecordingHandler(BaseHTTPRequestHandler):
    records: list[tuple[str, str, object]] = []
    poll_payload: object = None
    plain_workflow_id = False
    ack_missing = False

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def _body(self) -> object:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length)) if length else None

    def _send(self, value: object) -> None:
        data = json.dumps(value).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_plain(self, value: str) -> None:
        data = value.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        body = self._body()
        self.records.append(("POST", self.path, body))
        if self.path == "/api/workflow":
            if self.plain_workflow_id:
                self._send_plain("workflow-123")
            else:
                self._send("workflow-123")
        elif "/ack?" in self.path:
            if self.ack_missing:
                self.send_error(404)
            else:
                self._send(True)
        else:
            self._send({})

    def do_PUT(self) -> None:
        body = self._body()
        self.records.append(("PUT", self.path, body))
        self._send({})

    def do_GET(self) -> None:
        self.records.append(("GET", self.path, None))
        if self.path.startswith("/api/tasks/poll/"):
            payload = self.poll_payload
            type(self).poll_payload = None
            self._send(payload)
        else:
            self._send({"workflowId": "workflow-123", "status": "COMPLETED"})


class ConductorClientTests(unittest.TestCase):
    def setUp(self) -> None:
        RecordingHandler.records = []
        RecordingHandler.poll_payload = None
        RecordingHandler.plain_workflow_id = False
        RecordingHandler.ack_missing = False
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), RecordingHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_worker_can_exclude_gpu_tasks_from_general_polling(self) -> None:
        selected = select_task_types(None, ["insomnia_media_pipeline_tts", "insomnia_media_pipeline_music"])

        self.assertEqual(len(TASK_TYPES) - 2, len(selected))
        self.assertNotIn("insomnia_media_pipeline_tts", selected)
        self.assertNotIn("insomnia_media_pipeline_music", selected)

    def test_register_launch_and_monitor_use_conductor_contract(self) -> None:
        self.assertEqual(self.url + "/api", normalize_api_url(self.url))
        registered = register_definitions(self.url, ROOT / "conductor" / "definitions")
        self.assertEqual({"tasks": 15, "workflows": 1}, registered)

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            story, config = make_project(base)
            run_dir = base / "run"
            started = start_workflow(self.url, story, config, run_dir)
            self.assertEqual("workflow-123", started["workflow_id"])
            self.assertFalse(run_dir.exists())

        monitored = get_workflow(self.url, "workflow-123")
        self.assertEqual("COMPLETED", monitored["status"])
        methods_paths = [(method, path) for method, path, _ in RecordingHandler.records]
        self.assertIn(("POST", "/api/metadata/taskdefs"), methods_paths)
        self.assertIn(("PUT", "/api/metadata/workflow?overwrite=true"), methods_paths)
        self.assertIn(("POST", "/api/workflow"), methods_paths)
        self.assertIn(("GET", "/api/workflow/workflow-123?includeTasks=true"), methods_paths)

        launch_body = next(body for method, path, body in RecordingHandler.records if path == "/api/workflow")
        self.assertEqual(
            {"storyPath", "configPath", "runDir"},
            set(launch_body["input"]),
        )

    def test_launch_accepts_conductor_plain_text_workflow_identifier(self) -> None:
        RecordingHandler.plain_workflow_id = True
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            story, config = make_project(base)

            started = start_workflow(self.url, story, config, base / "run")

        self.assertEqual("workflow-123", started["workflow_id"])

    def test_worker_polls_executes_and_reports_one_artifact_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            story, config = make_project(base)
            run_dir = base / "run"
            RecordingHandler.poll_payload = {
                "taskId": "task-1",
                "workflowInstanceId": "workflow-123",
                "inputData": {
                    "payload": {
                        "stage": "init",
                        "storyPath": str(story),
                        "configPath": str(config),
                        "runDir": str(run_dir),
                    }
                },
            }

            result = poll_once(self.url, ["insomnia_media_pipeline_init"], "test-worker")

            self.assertEqual("COMPLETED", result["status"])
            self.assertTrue((run_dir / "run.json").is_file())
            update = next(
                body
                for method, path, body in RecordingHandler.records
                if method == "POST" and path == "/api/tasks"
            )
            self.assertEqual("COMPLETED", update["status"])
            self.assertEqual("init", update["outputData"]["stage"])

    def test_worker_supports_conductor_without_ack_endpoint(self) -> None:
        RecordingHandler.ack_missing = True
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            story, config = make_project(base)
            run_dir = base / "run"
            RecordingHandler.poll_payload = {
                "taskId": "task-1",
                "workflowInstanceId": "workflow-123",
                "inputData": {
                    "payload": {
                        "stage": "init",
                        "storyPath": str(story),
                        "configPath": str(config),
                        "runDir": str(run_dir),
                    }
                },
            }

            result = poll_once(self.url, ["insomnia_media_pipeline_init"], "test-worker")

        self.assertEqual("COMPLETED", result["status"])


if __name__ == "__main__":
    unittest.main()
