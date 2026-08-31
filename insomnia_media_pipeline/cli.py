"""Command-line interface for local validation and artifact-driven execution."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from .config import ConfigError, load_project_config
from .pipeline import PipelineError, dry_run, resume_pipeline, run_pipeline, run_stage, run_status
from .preflight import run_preflight


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _emit(value: Any, *, stream: Any = sys.stdout) -> None:
    print(json.dumps(_jsonable(value), indent=2, sort_keys=True), file=stream)


def _input_arguments(parser: argparse.ArgumentParser, *, include_run_dir: bool = True) -> None:
    parser.add_argument("--story", type=Path, required=True, help="Caller-supplied UTF-8 story")
    parser.add_argument("--config", type=Path, required=True, help="YAML or JSON project configuration")
    if include_run_dir:
        parser.add_argument("--run-dir", type=Path, required=True, help="New local artifact directory")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="insomnia-media-pipeline")
    subparsers = parser.add_subparsers(dest="command", required=True)
    preflight = subparsers.add_parser("preflight", help="Validate inputs without contacting providers")
    _input_arguments(preflight, include_run_dir=False)
    dry = subparsers.add_parser("dry-run", help="Validate and print the stage plan without writes")
    _input_arguments(dry)
    run = subparsers.add_parser("run", help="Run using runtime.mode from the live config")
    _input_arguments(run)
    synthetic = subparsers.add_parser("synthetic-run", help="Run the provider-free fake-media pipeline")
    _input_arguments(synthetic)
    resume = subparsers.add_parser("resume", help="Resume an initialized artifact directory")
    resume.add_argument("--run-dir", type=Path, required=True)
    status = subparsers.add_parser("status", help="Verify current local stage and package state")
    status.add_argument("--run-dir", type=Path, required=True)
    stage = subparsers.add_parser("stage", help="Execute one stage from run-directory artifacts")
    stage.add_argument("--run-dir", type=Path, required=True)
    stage.add_argument("--name", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "preflight":
            result = run_preflight(args.story, args.config)
        elif args.command == "dry-run":
            result = dry_run(args.story, args.config, args.run_dir)
        elif args.command == "run":
            config = load_project_config(args.config)
            if config.mode == "real":
                raise ConfigError("real mode must be launched through Conductor; use python3 -m conductor.client launch")
            result = run_pipeline(args.story, args.config, args.run_dir)
        elif args.command == "synthetic-run":
            config = load_project_config(args.config)
            if config.mode != "synthetic":
                raise ConfigError("synthetic-run requires runtime.mode: synthetic")
            result = run_pipeline(args.story, args.config, args.run_dir)
        elif args.command == "resume":
            result = resume_pipeline(args.run_dir)
        elif args.command == "status":
            result = run_status(args.run_dir)
        elif args.command == "stage":
            result = run_stage(args.run_dir, args.name)
        else:  # pragma: no cover - argparse enforces the command set
            raise PipelineError(f"unsupported command: {args.command}")
    except (ConfigError, PipelineError, OSError, ValueError) as exc:
        _emit({"status": "ERROR", "error": str(exc)}, stream=sys.stderr)
        return 2
    _emit(result)
    return 0
