from __future__ import annotations

import json
import wave
from pathlib import Path


PROMPT_NAMES = (
    "authoring",
    "authoring_checks",
    "pacing",
    "scene_selection",
    "scene_render",
    "thumbnail",
    "music_brief",
)

REQUIRED_FIELD = {
    "authoring": "story",
    "authoring_checks": "story",
    "pacing": "story",
    "scene_selection": "story",
    "scene_render": "scene",
    "thumbnail": "story",
    "music_brief": "direction",
}


def write_voice(path: Path, duration: float = 0.5) -> None:
    frames = int(16_000 * duration)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(b"\x00\x00" * frames)


def valid_config(prompt_dir: str = "prompts") -> dict:
    return {
        "project": {"name": "Neighborhood Field Day"},
        "voice": {
            "reference_audio": "voice.wav",
            "exaggeration": 0.45,
            "cfg_weight": 0.5,
            "temperature": 0.8,
            "tempo": 1.05,
        },
        "music": {
            "direction": "Warm acoustic motion with a bright finish",
            "model": "facebook/musicgen-small",
            "segment_duration": 30,
            "overlap": 5,
        },
        "prompts": {"directory": prompt_dir},
        "runtime": {"mode": "synthetic", "retries": 2},
    }


def make_project(root: Path) -> tuple[Path, Path]:
    story = root / "caller-story.txt"
    story.write_text(
        "On Saturday, neighbors gathered beside the library to build a community garden. "
        "They traded seedlings, painted signs, and celebrated when the first sunflower opened.\n",
        encoding="utf-8",
    )
    write_voice(root / "voice.wav")
    prompt_dir = root / "prompts"
    prompt_dir.mkdir()
    for name in PROMPT_NAMES:
        field = REQUIRED_FIELD[name]
        text = f"Policy for {name}: {{{field}}}\n"
        if name == "music_brief":
            text += "Story: {story}\n"
        (prompt_dir / f"{name}.txt").write_text(text, encoding="utf-8")
    config_path = root / "project.json"
    config_path.write_text(json.dumps(valid_config()), encoding="utf-8")
    return story, config_path
