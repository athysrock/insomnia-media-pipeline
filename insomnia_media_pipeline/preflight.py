"""Provider-free validation performed before a durable workflow is launched."""

from __future__ import annotations

import wave
from pathlib import Path
from typing import Any

from .config import ConfigError, load_project_config
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


def run_preflight(story_path: str | Path, config_path: str | Path) -> dict[str, Any]:
    story = Path(story_path).expanduser().resolve()
    word_count = _validate_story(story)
    config = load_project_config(config_path)
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
