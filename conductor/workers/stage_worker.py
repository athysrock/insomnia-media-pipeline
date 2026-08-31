"""Thin Conductor task boundary for stateless artifact reconstruction."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from insomnia_media_pipeline.pipeline import initialize_run, run_stage


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def execute(payload: dict[str, Any]) -> dict[str, Any]:
    stage = payload.get("stage")
    run_dir = payload.get("runDir")
    if not isinstance(stage, str) or not isinstance(run_dir, str):
        raise ValueError("payload requires string stage and runDir")
    if stage == "init":
        story = payload.get("storyPath")
        config = payload.get("configPath")
        if not isinstance(story, str) or not isinstance(config, str):
            raise ValueError("init payload requires string storyPath and configPath")
        result = initialize_run(story, config, run_dir)
    else:
        result = run_stage(run_dir, stage)
    if stage == "finalize":
        result["package"] = str((Path(run_dir).resolve() / "package.json"))
    return _jsonable(result)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", help="JSON object; defaults to standard input")
    args = parser.parse_args()
    raw = args.payload if args.payload is not None else sys.stdin.read()
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    print(json.dumps(execute(payload), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
