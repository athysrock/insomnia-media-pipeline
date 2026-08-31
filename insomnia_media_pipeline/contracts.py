"""Stable stage order and on-disk artifact contracts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StageContract:
    name: str
    dependencies: tuple[str, ...]
    outputs: tuple[str, ...]
    gpu: bool = False


STAGE_CONTRACTS: tuple[StageContract, ...] = (
    StageContract("init", (), ("run.json", "input/story.txt")),
    StageContract("authoring", ("input/story.txt",), ("authored/story.txt", "authored/checks.json")),
    StageContract("pacing", ("authored/story.txt",), ("pacing/plan.json", "pacing/paced.txt")),
    StageContract("scene_prompts", ("authored/story.txt", "pacing/plan.json"), ("visuals/scenes.json",)),
    StageContract("tts", ("pacing/paced.txt",), ("audio/narration.wav",), gpu=True),
    StageContract("music_brief", ("authored/story.txt",), ("music/brief.json",)),
    StageContract("music", ("music/brief.json", "audio/narration.wav"), ("music/music.wav",), gpu=True),
    StageContract("mix", ("audio/narration.wav", "music/music.wav"), ("audio/mix.wav",)),
    StageContract("images", ("visuals/scenes.json",), ("images/index.json", "staging/checkpoints/images.json"), gpu=True),
    StageContract("captions", ("audio/mix.wav", "pacing/plan.json"), ("captions/raw.srt",), gpu=True),
    StageContract("caption_postprocess", ("captions/raw.srt", "pacing/plan.json"), ("captions/final.srt",)),
    StageContract("video", ("audio/mix.wav", "images/index.json", "captions/final.srt"), ("video/video.mp4",)),
    StageContract("thumbnail", ("authored/story.txt", "visuals/scenes.json"), ("thumbnail/thumbnail.png",)),
    StageContract(
        "audit",
        ("video/video.mp4", "thumbnail/thumbnail.png", "audio/mix.wav", "captions/final.srt"),
        ("audit/audit.json",),
    ),
    StageContract(
        "finalize",
        ("audit/audit.json", "video/video.mp4", "thumbnail/thumbnail.png", "audio/mix.wav", "captions/final.srt"),
        ("package.json",),
    ),
)

CONTRACT_BY_NAME = {contract.name: contract for contract in STAGE_CONTRACTS}
