"""External prompt templates; templates are deliberately never cached."""

from __future__ import annotations

import string
from pathlib import Path

from .config import ConfigError, ProjectConfig


PROMPT_FIELDS: dict[str, frozenset[str]] = {
    "authoring": frozenset({"story"}),
    "authoring_checks": frozenset({"story"}),
    "pacing": frozenset({"story"}),
    "scene_selection": frozenset({"story"}),
    "scene_render": frozenset({"scene"}),
    "thumbnail": frozenset({"story"}),
    "music_brief": frozenset({"story", "direction"}),
}


def prompt_path(config: ProjectConfig, name: str) -> Path:
    if name not in PROMPT_FIELDS:
        raise ConfigError(f"unknown prompt name: {name}")
    return config.prompts_directory / f"{name}.txt"


def read_prompt(config: ProjectConfig, name: str) -> str:
    """Read and validate a template now, not when the run was initialized."""

    path = prompt_path(config, name)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ConfigError(f"cannot read prompt template {path}: {exc}") from exc
    if not text.strip():
        raise ConfigError(f"prompt template is empty: {path}")
    try:
        fields = {
            field_name
            for _, field_name, _, _ in string.Formatter().parse(text)
            if field_name is not None
        }
    except ValueError as exc:
        raise ConfigError(f"prompt template has invalid braces: {path}: {exc}") from exc
    expected = PROMPT_FIELDS[name]
    missing = expected - fields
    unknown = fields - expected
    if missing:
        raise ConfigError(f"prompt template {path} is missing field(s): {', '.join(sorted(missing))}")
    if unknown:
        raise ConfigError(f"prompt template {path} has unknown field(s): {', '.join(sorted(unknown))}")
    return text


def render_prompt(config: ProjectConfig, name: str, **values: str) -> str:
    template = read_prompt(config, name)
    expected = PROMPT_FIELDS[name]
    if set(values) != set(expected):
        raise ConfigError(f"prompt {name} requires exactly: {', '.join(sorted(expected))}")
    return template.format(**values)
