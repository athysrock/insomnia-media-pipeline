"""Public artifact-driven orchestration API."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from .artifacts import atomic_write_json, read_json, sha256_file
from .config import ConfigError, load_project_config
from .contracts import CONTRACT_BY_NAME, STAGE_CONTRACTS
from .preflight import _validate_real_providers, run_preflight
from .stages import HANDLERS


class PipelineError(RuntimeError):
    pass


STAGE_NAMES = tuple(contract.name for contract in STAGE_CONTRACTS)


def _relative_files(run_dir: Path, names: tuple[str, ...]) -> list[Path]:
    return [run_dir / name for name in names]


def _hashes(run_dir: Path, names: tuple[str, ...]) -> dict[str, str]:
    return {name: sha256_file(run_dir / name) for name in names}


def _receipt_path(run_dir: Path, stage_name: str) -> Path:
    return run_dir / "staging" / "receipts" / f"{stage_name}.json"


def _dynamic_outputs_valid(run_dir: Path, stage_name: str) -> bool:
    if stage_name != "images":
        return True
    try:
        index = read_json(run_dir / "images" / "index.json")
        checkpoint = read_json(run_dir / "staging" / "checkpoints" / "images.json")
        entries = index["images"]
        if checkpoint.get("status") != "COMPLETED" or not isinstance(entries, list) or not entries:
            return False
        expected = []
        for entry in entries:
            relative = entry["path"]
            candidate = (run_dir / relative).resolve()
            candidate.relative_to(run_dir)
            if not candidate.is_file() or sha256_file(candidate) != entry["sha256"]:
                return False
            expected.append(relative)
        return checkpoint.get("expected") == expected and checkpoint.get("completed") == expected
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _can_reuse(run_dir: Path, stage_name: str) -> bool:
    contract = CONTRACT_BY_NAME[stage_name]
    receipt_path = _receipt_path(run_dir, stage_name)
    if not receipt_path.is_file():
        return False
    try:
        receipt = read_json(receipt_path)
        return (
            receipt.get("stage") == stage_name
            and receipt.get("input_artifacts") == _hashes(run_dir, contract.dependencies)
            and receipt.get("output_artifacts") == _hashes(run_dir, contract.outputs)
            and _dynamic_outputs_valid(run_dir, stage_name)
        )
    except (OSError, ValueError, KeyError):
        return False


def dry_run(story_path: str | Path, config_path: str | Path, run_dir: str | Path) -> dict[str, Any]:
    report = run_preflight(story_path, config_path)
    return {
        "status": report["status"],
        "kind": "DRY_RUN",
        "run_dir": str(Path(run_dir).expanduser().resolve()),
        "stages": list(STAGE_NAMES),
        "provider_execution": False,
        "writes_performed": False,
    }


def initialize_run(story_path: str | Path, config_path: str | Path, run_dir: str | Path) -> dict[str, Any]:
    report = run_preflight(story_path, config_path)
    target = Path(run_dir).expanduser().resolve()
    if target.exists() and any(target.iterdir()):
        raise PipelineError(f"run directory must be empty: {target}")
    target.mkdir(parents=True, exist_ok=True)
    story_artifact = target / "input" / "story.txt"
    story_artifact.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(Path(story_path).expanduser().resolve(), story_artifact)
    manifest = {
        "schema_version": 1,
        "config_path": str(Path(config_path).expanduser().resolve()),
        "story_artifact": "input/story.txt",
        "stage_sequence": list(STAGE_NAMES),
        "live_inputs": {
            "config": "read from config_path at every executed stage",
            "prompts": "read from the current config at every prompt-driven stage",
            "resume_invalidation": "excluded",
        },
    }
    atomic_write_json(target / "run.json", manifest)
    contract = CONTRACT_BY_NAME["init"]
    receipt = {
        "stage": "init",
        "status": "COMPLETED",
        "input_artifacts": {},
        "output_artifacts": _hashes(target, contract.outputs),
    }
    atomic_write_json(_receipt_path(target, "init"), receipt)
    return {"status": "COMPLETED", "stage": "init", "run_dir": str(target), "preflight": report}


def _load_run(run_dir: Path) -> tuple[dict[str, Any], Any]:
    manifest_path = run_dir / "run.json"
    if not manifest_path.is_file():
        raise PipelineError(f"run manifest does not exist: {manifest_path}")
    try:
        manifest = read_json(manifest_path)
        config = load_project_config(manifest["config_path"])
        if config.mode == "real":
            _validate_real_providers(config)
    except (OSError, ValueError, KeyError, ConfigError) as exc:
        raise PipelineError(f"cannot reconstruct run context: {exc}") from exc
    return manifest, config


def run_stage(run_dir: str | Path, stage_name: str) -> dict[str, Any]:
    target = Path(run_dir).expanduser().resolve()
    _, config = _load_run(target)
    if stage_name not in CONTRACT_BY_NAME or stage_name == "init":
        raise PipelineError(f"unknown executable stage: {stage_name}")
    contract = CONTRACT_BY_NAME[stage_name]
    for dependency in _relative_files(target, contract.dependencies):
        if not dependency.is_file():
            raise PipelineError(f"stage {stage_name} requires artifact: {dependency.relative_to(target)}")
    if _can_reuse(target, stage_name):
        return {"status": "REUSED", "stage": stage_name, "run_dir": str(target)}
    handler = HANDLERS.get(stage_name)
    if handler is None:
        raise PipelineError(f"stage is not implemented yet: {stage_name}")

    last_error: Exception | None = None
    for _attempt in range(config.retries + 1):
        try:
            handler(target, config)
            last_error = None
            break
        except Exception as exc:  # stage boundary owns bounded retry
            last_error = exc
    if last_error is not None:
        raise PipelineError(f"stage {stage_name} failed after {config.retries + 1} attempt(s): {last_error}") from last_error
    missing = [path for path in _relative_files(target, contract.outputs) if not path.is_file()]
    if missing:
        raise PipelineError(f"stage {stage_name} did not satisfy output contract: {missing[0].relative_to(target)}")
    receipt = {
        "stage": stage_name,
        "status": "COMPLETED",
        "input_artifacts": _hashes(target, contract.dependencies),
        "output_artifacts": _hashes(target, contract.outputs),
    }
    atomic_write_json(_receipt_path(target, stage_name), receipt)
    return {"status": "COMPLETED", "stage": stage_name, "run_dir": str(target)}


def run_pipeline(story_path: str | Path, config_path: str | Path, run_dir: str | Path) -> dict[str, Any]:
    initialize_run(story_path, config_path, run_dir)
    results = []
    for name in STAGE_NAMES[1:]:
        results.append(run_stage(run_dir, name))
    package = read_json(Path(run_dir) / "package.json")
    return {"status": package["status"], "run_dir": str(Path(run_dir).resolve()), "stages": results}


def resume_pipeline(run_dir: str | Path) -> dict[str, Any]:
    """Resume an initialized run by invoking each stateless stage in order."""

    results = [run_stage(run_dir, name) for name in STAGE_NAMES[1:]]
    package = read_json(Path(run_dir) / "package.json")
    return {"status": package["status"], "run_dir": str(Path(run_dir).resolve()), "stages": results}


def run_status(run_dir: str | Path) -> dict[str, Any]:
    """Read and validate receipts without executing or repairing any stage."""

    target = Path(run_dir).expanduser().resolve()
    _load_run(target)
    stages = []
    for contract in STAGE_CONTRACTS:
        reusable = _can_reuse(target, contract.name)
        stages.append({"name": contract.name, "status": "COMPLETE" if reusable else "INCOMPLETE"})
    package_path = target / "package.json"
    package = read_json(package_path) if package_path.is_file() else {}
    passed = all(item["status"] == "COMPLETE" for item in stages) and package.get("status") == "PASS"
    return {
        "status": "PASS" if passed else "INCOMPLETE",
        "run_dir": str(target),
        "stages": stages,
        "package": "package.json" if package_path.is_file() else None,
    }
