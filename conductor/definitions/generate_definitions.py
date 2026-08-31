#!/usr/bin/env python3
"""Generate deterministic Conductor task and workflow definitions."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from insomnia_media_pipeline.contracts import STAGE_CONTRACTS


DEFINITIONS = Path(__file__).resolve().parent
WORKFLOWS = DEFINITIONS / "workflows"
OWNER = "media-pipeline@local.invalid"
WORKFLOW_NAME = "insomnia_media_pipeline_v1"

TIMEOUTS = {
    "init": 300,
    "authoring": 1800,
    "pacing": 1200,
    "scene_prompts": 1200,
    "tts": 2400,
    "music_brief": 900,
    "music": 2400,
    "mix": 600,
    "images": 5400,
    "captions": 1800,
    "caption_postprocess": 300,
    "video": 2400,
    "thumbnail": 600,
    "audit": 600,
    "finalize": 300,
}


def _task_name(stage: str) -> str:
    return f"insomnia_media_pipeline_{stage}"


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def task_definition(stage: str) -> dict[str, Any]:
    timeout = TIMEOUTS[stage]
    return {
        "backoffScaleFactor": 1,
        "concurrentExecLimit": 1,
        "description": f"Execute the artifact-driven {stage} stage.",
        "inputKeys": ["payload"],
        "inputTemplate": {},
        "name": _task_name(stage),
        "outputKeys": [],
        "ownerEmail": OWNER,
        "pollTimeoutSeconds": 30,
        "rateLimitFrequencyInSeconds": 1,
        "rateLimitPerFrequency": 0,
        "responseTimeoutSeconds": min(300, timeout),
        "retryCount": 0,
        "retryDelaySeconds": 30,
        "retryLogic": "FIXED",
        "timeoutPolicy": "RETRY",
        "timeoutSeconds": timeout,
    }


def workflow_task(stage: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "runDir": "${workflow.input.runDir}",
        "stage": stage,
    }
    if stage == "init":
        payload.update(
            {
                "storyPath": "${workflow.input.storyPath}",
                "configPath": "${workflow.input.configPath}",
            }
        )
    return {
        "inputParameters": {"payload": payload},
        "name": _task_name(stage),
        "taskReferenceName": f"stage_{stage}",
        "type": "SIMPLE",
    }


def generate() -> tuple[Path, Path]:
    stages = [contract.name for contract in STAGE_CONTRACTS]
    task_path = DEFINITIONS / "task-definitions.json"
    workflow_path = WORKFLOWS / f"{WORKFLOW_NAME}.json"
    _write_json(task_path, [task_definition(stage) for stage in stages])
    _write_json(
        workflow_path,
        {
            "description": "One-story local media package with deterministic audit and finalize.",
            "inputParameters": ["storyPath", "configPath", "runDir"],
            "name": WORKFLOW_NAME,
            "outputParameters": {
                "package": "${stage_finalize.output.package}",
                "runDir": "${workflow.input.runDir}",
                "status": "${stage_finalize.output.status}",
            },
            "ownerEmail": OWNER,
            "restartable": True,
            "schemaVersion": 2,
            "tasks": [workflow_task(stage) for stage in stages],
            "timeoutPolicy": "ALERT_ONLY",
            "timeoutSeconds": sum(TIMEOUTS.values()) + 1800,
            "version": 1,
            "workflowStatusListenerEnabled": False,
        },
    )
    return task_path, workflow_path


if __name__ == "__main__":
    generate()
