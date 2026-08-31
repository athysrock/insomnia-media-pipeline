"""Provider-free validation performed before a durable workflow is launched."""

from __future__ import annotations

import json
import urllib.parse
import wave
from pathlib import Path
from typing import Any

from .config import ConfigError, ProjectConfig, load_project_config
from .deadlines import MAX_COMFYUI_TIMEOUT_SECONDS, MAX_LLM_TIMEOUT_SECONDS
from .prompts import PROMPT_FIELDS, read_prompt


def _validate_story(path: Path) -> int:
    if not path.is_file():
        raise ConfigError(f"story does not exist: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"story must be readable UTF-8 text: {exc}") from exc
    if not text.strip():
        raise ConfigError("story must not be empty")
    return len(text.split())


def _inspect_voice(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"voice.reference_audio does not exist: {path}")
    if path.suffix.lower() != ".wav":
        raise ConfigError("voice.reference_audio must be a valid PCM WAV file")
    try:
        with wave.open(str(path), "rb") as audio:
            channels = audio.getnchannels()
            sample_width = audio.getsampwidth()
            sample_rate = audio.getframerate()
            frame_count = audio.getnframes()
            compression = audio.getcomptype()
    except (OSError, EOFError, wave.Error) as exc:
        raise ConfigError(f"voice.reference_audio must be a valid PCM WAV file: {exc}") from exc
    duration = frame_count / sample_rate if sample_rate else 0.0
    if compression != "NONE" or channels not in {1, 2} or sample_width not in {1, 2, 3, 4}:
        raise ConfigError("voice.reference_audio must be a valid PCM WAV file")
    if not 0.1 <= duration <= 300.0:
        raise ConfigError("voice.reference_audio duration must be between 0.1 and 300 seconds")
    return {
        "duration": duration,
        "channels": channels,
        "sample_width": sample_width,
        "sample_rate": sample_rate,
    }


def _bounded_timeout(section: dict[str, Any], dotted: str, default: int, maximum: int) -> None:
    value = section.get("timeout_seconds", default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ConfigError(f"{dotted} must be an integer between 1 and {maximum}")


def _validate_real_providers(config: ProjectConfig) -> None:
    llm = config.raw.get("llm")
    if not isinstance(llm, dict) or not isinstance(llm.get("command"), str) or not llm["command"].strip():
        raise ConfigError("llm.command is required and must be non-empty text in real mode")
    _bounded_timeout(llm, "llm.timeout_seconds", 900, MAX_LLM_TIMEOUT_SECONDS)

    comfyui = config.raw.get("comfyui")
    if not isinstance(comfyui, dict) or not isinstance(comfyui.get("api_url"), str) or not comfyui["api_url"].strip():
        raise ConfigError("comfyui.api_url is required and must be non-empty text in real mode")
    parsed_url = urllib.parse.urlsplit(comfyui["api_url"])
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ConfigError("comfyui.api_url must be an absolute http or https URL")
    _bounded_timeout(comfyui, "comfyui.timeout_seconds", 300, MAX_COMFYUI_TIMEOUT_SECONDS)

    workflow = comfyui.get("workflow_template")
    if not isinstance(workflow, str) or not workflow.strip():
        raise ConfigError("comfyui.workflow_template is required and must be non-empty text in real mode")
    workflow_path = (config.path.parent / workflow).resolve()
    try:
        template = json.loads(workflow_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigError(f"comfyui.workflow_template must be a readable JSON file: {exc}") from exc
    if not isinstance(template, dict) or not template:
        raise ConfigError("comfyui.workflow_template must contain a non-empty JSON object")
    if config.retries != 0:
        raise ConfigError("runtime.retries must be 0 in real mode")


def run_preflight(story_path: str | Path, config_path: str | Path) -> dict[str, Any]:
    story = Path(story_path).expanduser().resolve()
    word_count = _validate_story(story)
    config = load_project_config(config_path)
    if config.mode == "real":
        _validate_real_providers(config)
    voice = _inspect_voice(config.voice.reference_audio)
    if not config.prompts_directory.is_dir():
        raise ConfigError(f"prompts.directory does not exist: {config.prompts_directory}")
    checked = []
    for name in PROMPT_FIELDS:
        read_prompt(config, name)
        checked.append(name)
    return {
        "status": "PASS",
        "story_path": story,
        "config_path": config.path,
        "story_word_count": word_count,
        "voice_path": config.voice.reference_audio,
        "voice_duration_seconds": voice["duration"],
        "voice_format": {
            "container": "wav",
            "codec": "pcm",
            "channels": voice["channels"],
            "sample_width_bytes": voice["sample_width"],
            "sample_rate": voice["sample_rate"],
        },
        "prompts_checked": checked,
        "runtime_mode": config.mode,
    }
