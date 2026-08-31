"""Small standard-library client for Conductor metadata and workflows."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Sequence

from insomnia_media_pipeline.preflight import run_preflight


WORKFLOW_NAME = "insomnia_media_pipeline_v1"
WORKFLOW_VERSION = 1


class ConductorError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def normalize_api_url(value: str) -> str:
    url = value.rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise ConductorError("Conductor URL must use http or https")
    return url if url.endswith("/api") else url + "/api"


def _request(method: str, url: str, payload: Any | None = None) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        # All callers normalize the base URL to HTTP(S) before reaching this request boundary.
        with urllib.request.urlopen(request, timeout=60) as response:  # nosec B310
            content_type = response.headers.get_content_type()
            body = response.read().decode("utf-8").strip()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[-1000:]
        raise ConductorError(
            f"Conductor {method} failed with HTTP {exc.code}: {detail}",
            status_code=exc.code,
        ) from exc
    except urllib.error.URLError as exc:
        raise ConductorError(f"cannot reach Conductor: {exc.reason}") from exc
    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        if content_type == "text/plain":
            return body
        raise ConductorError("Conductor returned invalid JSON") from exc


def register_definitions(server_url: str, definitions_dir: str | Path) -> dict[str, int]:
    base = normalize_api_url(server_url)
    definitions = Path(definitions_dir).resolve()
    tasks = json.loads((definitions / "task-definitions.json").read_text(encoding="utf-8"))
    workflow_paths = sorted((definitions / "workflows").glob("*.json"))
    workflows = [json.loads(path.read_text(encoding="utf-8")) for path in workflow_paths]
    if not isinstance(tasks, list) or not tasks or not workflows:
        raise ConductorError("generated task and workflow definitions are required")
    _request("POST", base + "/metadata/taskdefs", tasks)
    _request("PUT", base + "/metadata/workflow?overwrite=true", workflows)
    return {"tasks": len(tasks), "workflows": len(workflows)}


def start_workflow(
    server_url: str,
    story_path: str | Path,
    config_path: str | Path,
    run_dir: str | Path,
) -> dict[str, Any]:
    story = Path(story_path).expanduser().resolve()
    config = Path(config_path).expanduser().resolve()
    output = Path(run_dir).expanduser().resolve()
    run_preflight(story, config)
    if output.exists() and any(output.iterdir()):
        raise ConductorError(f"run directory must be absent or empty: {output}")
    correlation = hashlib.sha256((str(story) + "\0" + str(output)).encode("utf-8")).hexdigest()[:20]
    body = {
        "name": WORKFLOW_NAME,
        "version": WORKFLOW_VERSION,
        "correlationId": f"local-media:{correlation}",
        "input": {
            "storyPath": str(story),
            "configPath": str(config),
            "runDir": str(output),
        },
    }
    workflow_id = _request("POST", normalize_api_url(server_url) + "/workflow", body)
    if not isinstance(workflow_id, str) or not workflow_id:
        raise ConductorError("Conductor did not return a workflow identifier")
    return {
        "status": "STARTED",
        "workflow_id": workflow_id,
        "workflow_name": WORKFLOW_NAME,
        "correlation_id": body["correlationId"],
        "run_dir": str(output),
    }


def get_workflow(server_url: str, workflow_id: str) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9._:-]+", workflow_id):
        raise ConductorError("invalid workflow identifier")
    result = _request(
        "GET",
        normalize_api_url(server_url) + f"/workflow/{workflow_id}?includeTasks=true",
    )
    if not isinstance(result, dict):
        raise ConductorError("Conductor workflow response must be an object")
    return result


def build_parser() -> argparse.ArgumentParser:
    default_url = os.environ.get("CONDUCTOR_SERVER_URL", "http://localhost:8080")
    parser = argparse.ArgumentParser(prog="python3 -m conductor.client")
    subparsers = parser.add_subparsers(dest="command", required=True)
    register = subparsers.add_parser("register")
    register.add_argument("--url", default=default_url)
    register.add_argument("--definitions", type=Path, default=Path(__file__).parent / "definitions")
    launch = subparsers.add_parser("launch")
    launch.add_argument("--url", default=default_url)
    launch.add_argument("--story", type=Path, required=True)
    launch.add_argument("--config", type=Path, required=True)
    launch.add_argument("--run-dir", type=Path, required=True)
    monitor = subparsers.add_parser("monitor")
    monitor.add_argument("--url", default=default_url)
    monitor.add_argument("--workflow-id", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "register":
            result = register_definitions(args.url, args.definitions)
        elif args.command == "launch":
            result = start_workflow(args.url, args.story, args.config, args.run_dir)
        else:
            result = get_workflow(args.url, args.workflow_id)
    except (ConductorError, OSError, ValueError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
