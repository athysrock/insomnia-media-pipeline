"""Polling worker for generated Conductor SIMPLE tasks."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Sequence

from insomnia_media_pipeline.contracts import STAGE_CONTRACTS

from .client import ConductorError, _request, normalize_api_url
from .workers.stage_worker import execute


TASK_TYPES = tuple(f"insomnia_media_pipeline_{contract.name}" for contract in STAGE_CONTRACTS)


def select_task_types(included: Sequence[str] | None, excluded: Sequence[str] | None) -> tuple[str, ...]:
    selected = tuple(included or TASK_TYPES)
    blocked = set(excluded or ())
    return tuple(task_type for task_type in selected if task_type not in blocked)


def _task_url(base: str, task_type: str, worker_id: str) -> str:
    return (
        base
        + "/tasks/poll/"
        + urllib.parse.quote(task_type, safe="")
        + "?workerid="
        + urllib.parse.quote(worker_id, safe="")
    )


def poll_once(server_url: str, task_types: Sequence[str], worker_id: str) -> dict[str, Any]:
    base = normalize_api_url(server_url)
    for task_type in task_types:
        task = _request("GET", _task_url(base, task_type, worker_id))
        if not isinstance(task, dict) or not task.get("taskId"):
            continue
        task_id = str(task["taskId"])
        workflow_id = str(task.get("workflowInstanceId", ""))
        try:
            acknowledged = _request(
                "POST",
                base
                + "/tasks/"
                + urllib.parse.quote(task_id, safe="")
                + "/ack?workerid="
                + urllib.parse.quote(worker_id, safe=""),
            )
        except ConductorError as exc:
            if exc.status_code != 404:
                raise
            acknowledged = None
        if acknowledged is False:
            return {"status": "NOT_ACKNOWLEDGED", "task_id": task_id}
        try:
            input_data = task.get("inputData", {})
            payload = input_data.get("payload") if isinstance(input_data, dict) else None
            if not isinstance(payload, dict):
                raise ValueError("task inputData.payload must be an object")
            output = execute(payload)
            status = "COMPLETED"
            reason = None
        except Exception as exc:  # report task failure to durable owner
            output = {}
            status = "FAILED"
            reason = str(exc)[:1000]
        update = {
            "outputData": output,
            "status": status,
            "taskId": task_id,
            "workerId": worker_id,
            "workflowInstanceId": workflow_id,
        }
        if reason is not None:
            update["reasonForIncompletion"] = reason
        _request("POST", base + "/tasks", update)
        return {"status": status, "task_id": task_id, "task_type": task_type}
    return {"status": "IDLE"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m conductor.worker")
    parser.add_argument("--url", default=os.environ.get("CONDUCTOR_SERVER_URL", "http://localhost:8080"))
    parser.add_argument("--worker-id", default=f"{socket.gethostname()}-{os.getpid()}")
    parser.add_argument("--task", action="append", dest="tasks", choices=TASK_TYPES)
    parser.add_argument("--exclude-task", action="append", dest="excluded_tasks", choices=TASK_TYPES)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-interval", type=float, default=1.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    tasks = select_task_types(args.tasks, args.excluded_tasks)
    if not tasks:
        print(json.dumps({"status": "ERROR", "error": "worker must poll at least one task type"}), file=sys.stderr)
        return 2
    if args.poll_interval < 0.1 or args.poll_interval > 60:
        print(json.dumps({"status": "ERROR", "error": "poll interval must be between 0.1 and 60"}), file=sys.stderr)
        return 2
    try:
        while True:
            result = poll_once(args.url, tasks, args.worker_id)
            if args.once:
                print(json.dumps(result, indent=2, sort_keys=True))
                return 0 if result["status"] != "FAILED" else 1
            if result["status"] == "IDLE":
                time.sleep(args.poll_interval)
    except (ConductorError, OSError, ValueError) as exc:
        print(json.dumps({"status": "ERROR", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
