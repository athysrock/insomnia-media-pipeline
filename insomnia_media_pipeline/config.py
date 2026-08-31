"""Strict project configuration loaded afresh at every stage boundary."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigError(ValueError):
    """Raised when a project input is missing, malformed, or unsupported."""


@dataclass(frozen=True)
class VoiceConfig:
    reference_audio: Path
    exaggeration: float
    cfg_weight: float
    temperature: float
    tempo: float


@dataclass(frozen=True)
class MusicConfig:
    direction: str
    model: str
    segment_duration: int
    overlap: int


@dataclass(frozen=True)
class ProjectConfig:
    path: Path
    project_name: str
    voice: VoiceConfig
    music: MusicConfig
    prompts_directory: Path
    mode: str
    retries: int
    raw: dict[str, Any]


_TOP_LEVEL_KEYS = {
    "project",
    "voice",
    "music",
    "prompts",
    "runtime",
    "llm",
    "comfyui",
    "captions",
    "video",
    "thumbnail",
    "mix",
    "tools",
}
_SECTION_KEYS = {
    "project": {"name"},
    "voice": {"reference_audio", "exaggeration", "cfg_weight", "temperature", "tempo"},
    "music": {"direction", "model", "segment_duration", "overlap"},
    "prompts": {"directory"},
    "runtime": {"mode", "retries"},
    "llm": {"command", "timeout_seconds"},
    "comfyui": {"api_url", "start_command", "stop_command", "workflow_template", "timeout_seconds"},
    "captions": {"command", "model", "language"},
    "video": {"width", "height", "fps", "crf"},
    "thumbnail": {"width", "height"},
    "mix": {"music_volume"},
    "tools": {"ffmpeg", "ffprobe"},
}

DEFAULT_VOICE_DELIVERY = {
    "exaggeration": 0.5,
    "cfg_weight": 0.5,
    "temperature": 0.8,
    "tempo": 0.9,
}


def _yaml_scalar(value: str, line_number: int) -> Any:
    value = value.strip()
    if not value:
        return {}
    if value[:1] in {'"', "'"}:
        if len(value) < 2 or value[-1] != value[0]:
            raise ConfigError(f"unterminated YAML string on line {line_number}")
        if value[0] == '"':
            try:
                return json.loads(value)
            except json.JSONDecodeError as exc:
                raise ConfigError(f"invalid YAML string on line {line_number}: {exc}") from exc
        return value[1:-1].replace("''", "'")
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "~"}:
        return None
    if re.fullmatch(r"[-+]?\d+", value):
        return int(value)
    if re.fullmatch(r"[-+]?(?:\d+\.\d*|\d*\.\d+)(?:[eE][-+]?\d+)?", value):
        return float(value)
    if value.startswith("[") or value.startswith("{"):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"invalid inline YAML value on line {line_number}: {exc}") from exc
    return value


def _load_simple_yaml(text: str) -> dict[str, Any]:
    """Parse the mapping-only YAML subset used by project configuration.

    Keeping the accepted subset small makes parsing deterministic without adding a
    mandatory runtime dependency. JSON-style inline lists and maps are supported.
    """

    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-2, root)]
    for line_number, original in enumerate(text.splitlines(), 1):
        if not original.strip() or original.lstrip().startswith("#"):
            continue
        if "\t" in original[: len(original) - len(original.lstrip())]:
            raise ConfigError(f"tabs are not allowed for YAML indentation (line {line_number})")
        indent = len(original) - len(original.lstrip(" "))
        if indent % 2:
            raise ConfigError(f"YAML indentation must use two-space steps (line {line_number})")
        content = original.strip()
        if content.startswith("-"):
            raise ConfigError(f"block YAML lists are not supported (line {line_number}); use an inline list")
        if ":" not in content:
            raise ConfigError(f"expected key: value on YAML line {line_number}")
        key, value = content.split(":", 1)
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key):
            raise ConfigError(f"invalid YAML key on line {line_number}: {key!r}")
        while stack and indent <= stack[-1][0]:
            stack.pop()
        if not stack or indent != stack[-1][0] + 2:
            raise ConfigError(f"invalid YAML nesting on line {line_number}")
        parent = stack[-1][1]
        if key in parent:
            raise ConfigError(f"duplicate YAML key on line {line_number}: {key}")
        parsed = _yaml_scalar(value, line_number)
        parent[key] = parsed
        if parsed == {} and not value.strip():
            stack.append((indent, parsed))
    return root


def _read_mapping(path: Path) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"cannot read project config {path}: {exc}") from exc
    try:
        if path.suffix.lower() == ".json":
            value = json.loads(text)
        elif path.suffix.lower() in {".yaml", ".yml"}:
            value = _load_simple_yaml(text)
        else:
            raise ConfigError("project config must use .json, .yaml, or .yml")
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON project config: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigError("project config root must be an object/mapping")
    return value


def _mapping(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name)
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be an object/mapping")
    unknown = sorted(set(value) - _SECTION_KEYS[name])
    if unknown:
        raise ConfigError(f"{name} has unknown key(s): {', '.join(unknown)}")
    return value


def _required_text(section: dict[str, Any], dotted: str, key: str) -> str:
    value = section.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{dotted} is required and must be non-empty text")
    return value.strip()


def _number(
    section: dict[str, Any], dotted: str, key: str, low: float, high: float, *, default: float | None = None
) -> float:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{dotted} must be a number")
    result = float(value)
    if not low <= result <= high:
        raise ConfigError(f"{dotted} must be between {low} and {high}")
    return result


def load_project_config(path: str | Path) -> ProjectConfig:
    """Load and validate a project config from disk on every call."""

    config_path = Path(path).expanduser().resolve()
    if not config_path.is_file():
        raise ConfigError(f"project config does not exist: {config_path}")
    raw = _read_mapping(config_path)
    unknown = sorted(set(raw) - _TOP_LEVEL_KEYS)
    if unknown:
        raise ConfigError(f"project config has unknown section(s): {', '.join(unknown)}")

    project = _mapping(raw, "project")
    voice = _mapping(raw, "voice")
    music = _mapping(raw, "music")
    prompts = _mapping(raw, "prompts")
    runtime = _mapping(raw, "runtime")
    for optional in _TOP_LEVEL_KEYS - {"project", "voice", "music", "prompts", "runtime"}:
        if optional in raw:
            _mapping(raw, optional)

    reference = _required_text(voice, "voice.reference_audio", "reference_audio")
    reference_path = (config_path.parent / reference).resolve()
    prompt_value = _required_text(prompts, "prompts.directory", "directory")
    prompt_path = (config_path.parent / prompt_value).resolve()
    mode = _required_text(runtime, "runtime.mode", "mode")
    if mode not in {"synthetic", "real"}:
        raise ConfigError("runtime.mode must be 'synthetic' or 'real'")
    retries = runtime.get("retries")
    if isinstance(retries, bool) or not isinstance(retries, int) or not 0 <= retries <= 10:
        raise ConfigError("runtime.retries must be an integer between 0 and 10")

    segment_duration = music.get("segment_duration")
    overlap = music.get("overlap")
    if isinstance(segment_duration, bool) or not isinstance(segment_duration, int) or segment_duration < 2:
        raise ConfigError("music.segment_duration must be an integer of at least 2")
    if isinstance(overlap, bool) or not isinstance(overlap, int) or not 1 <= overlap < segment_duration:
        raise ConfigError("music.overlap must be a positive integer smaller than music.segment_duration")

    return ProjectConfig(
        path=config_path,
        project_name=_required_text(project, "project.name", "name"),
        voice=VoiceConfig(
            reference_audio=reference_path,
            exaggeration=_number(voice, "voice.exaggeration", "exaggeration", 0.0, 2.0, default=DEFAULT_VOICE_DELIVERY["exaggeration"]),
            cfg_weight=_number(voice, "voice.cfg_weight", "cfg_weight", 0.0, 1.0, default=DEFAULT_VOICE_DELIVERY["cfg_weight"]),
            temperature=_number(voice, "voice.temperature", "temperature", 0.05, 5.0, default=DEFAULT_VOICE_DELIVERY["temperature"]),
            tempo=_number(voice, "voice.tempo", "tempo", 0.5, 2.0, default=DEFAULT_VOICE_DELIVERY["tempo"]),
        ),
        music=MusicConfig(
            direction=_required_text(music, "music.direction", "direction"),
            model=_required_text(music, "music.model", "model"),
            segment_duration=segment_duration,
            overlap=overlap,
        ),
        prompts_directory=prompt_path,
        mode=mode,
        retries=retries,
        raw=raw,
    )
