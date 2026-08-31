"""Pure-ish stage implementations that reconstruct context from run files."""

from __future__ import annotations

import json
import hashlib
import math
import os
import re
import struct
import subprocess
import wave
import zlib
from array import array
from collections import Counter
from pathlib import Path
from typing import Callable

from .artifacts import atomic_write_bytes, atomic_write_json, atomic_write_text, read_json, sha256_file
from .config import ProjectConfig
from .media import (
    ComfyUIClient,
    MediaError,
    ffmpeg_assemble_video,
    ffmpeg_mix,
    ffmpeg_thumbnail,
    generate_chatterbox_narration,
    generate_musicgen_track,
    run_llm_command,
    run_whisperx,
)
from .prompts import render_prompt


class StageExecutionError(RuntimeError):
    pass


def _story(run_dir: Path) -> str:
    return (run_dir / "authored" / "story.txt").read_text(encoding="utf-8")


def _section(config: ProjectConfig, name: str) -> dict:
    value = config.raw.get(name, {})
    return value if isinstance(value, dict) else {}


def _llm(config: ProjectConfig, prompt: str) -> str:
    llm = _section(config, "llm")
    command = llm.get("command")
    if not isinstance(command, str) or not command.strip():
        raise StageExecutionError("llm.command is required in real mode")
    timeout = llm.get("timeout_seconds", 900)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
        raise StageExecutionError("llm.timeout_seconds must be a positive integer")
    return run_llm_command(command, prompt, timeout)


def authoring(run_dir: Path, config: ProjectConfig) -> None:
    source = (run_dir / "input" / "story.txt").read_text(encoding="utf-8")
    authoring_prompt = render_prompt(config, "authoring", story=source)
    authored = _llm(config, authoring_prompt) if config.mode == "real" else source
    normalized = "\n".join(line.rstrip() for line in authored.strip().splitlines()) + "\n"
    render_prompt(config, "authoring_checks", story=normalized)
    words = re.findall(r"\b[\w'-]+\b", normalized, flags=re.UNICODE)
    checks = {
        "status": "PASS" if 5 <= len(words) <= 20_000 else "FAIL",
        "checks": {
            "non_empty": bool(normalized.strip()),
            "word_count_in_range": 5 <= len(words) <= 20_000,
            "utf8_text": True,
        },
        "word_count": len(words),
    }
    if checks["status"] != "PASS":
        raise StageExecutionError("deterministic authoring checks failed")
    atomic_write_text(run_dir / "authored" / "story.txt", normalized)
    atomic_write_json(run_dir / "authored" / "checks.json", checks)


def pacing(run_dir: Path, config: ProjectConfig) -> None:
    story = _story(run_dir)
    pacing_prompt = render_prompt(config, "pacing", story=story)
    paced_source = _llm(config, pacing_prompt) if config.mode == "real" else story
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", paced_source) if part.strip()]
    chunks = []
    elapsed = 0.0
    for index, sentence in enumerate(sentences, 1):
        word_count = len(sentence.split())
        duration = round(max(1.2, word_count / 2.5), 3)
        chunks.append(
            {
                "index": index,
                "text": sentence,
                "start_seconds": round(elapsed, 3),
                "duration_seconds": duration,
                "pause_after_seconds": 0.45,
            }
        )
        elapsed += duration + 0.45
    if not chunks:
        raise StageExecutionError("pacing produced no chunks")
    atomic_write_json(
        run_dir / "pacing" / "plan.json",
        {"chunks": chunks, "estimated_duration_seconds": round(elapsed, 3)},
    )
    paced = "\n\n".join(chunk["text"] for chunk in chunks) + "\n"
    atomic_write_text(run_dir / "pacing" / "paced.txt", paced)


def _scene_assignments(chunks: list[dict], scene_count: int) -> list[dict]:
    if scene_count < 1 or scene_count > len(chunks):
        raise StageExecutionError("scene count must be between one and the pacing chunk count")
    assignments = []
    for index in range(scene_count):
        start = index * len(chunks) // scene_count
        end = (index + 1) * len(chunks) // scene_count
        span = chunks[start:end]
        assignments.append(
            {
                "source_chunk": span[0]["index"],
                "duration_seconds": sum(
                    float(chunk["duration_seconds"]) + float(chunk["pause_after_seconds"])
                    for chunk in span
                ),
            }
        )
    return assignments


def scene_prompts(run_dir: Path, config: ProjectConfig) -> None:
    story = _story(run_dir)
    selection_prompt = render_prompt(config, "scene_selection", story=story)
    plan = json.loads((run_dir / "pacing" / "plan.json").read_text(encoding="utf-8"))
    chunks = plan["chunks"]
    scenes = []
    if config.mode == "real":
        selected = [line.strip(" -*\t") for line in _llm(config, selection_prompt).splitlines() if line.strip(" -*\t")]
        scene_sources = selected[:len(chunks)] or [chunk["text"] for chunk in chunks]
    else:
        scene_sources = [chunk["text"] for chunk in chunks]
    assignments = _scene_assignments(chunks, len(scene_sources))
    for index, scene in enumerate(scene_sources, 1):
        assignment = assignments[index - 1]
        render_request = render_prompt(config, "scene_render", scene=scene)
        if config.mode == "real":
            rendered = _llm(config, render_request).strip()
        else:
            rendered = f"Cinematic documentary scene, natural light, clear subject and action: {scene}. No visible text."
        scenes.append(
            {
                "index": index,
                "source_chunk": assignment["source_chunk"],
                "duration_seconds": assignment["duration_seconds"],
                "prompt": rendered,
                "image": f"images/scene_{index:03d}.png",
            }
        )
    atomic_write_json(run_dir / "visuals" / "scenes.json", {"scenes": scenes})


_STOP_WORDS = {
    "about", "after", "again", "along", "also", "and", "before", "beside", "build",
    "for", "from", "into", "its", "opened", "that", "the", "their", "they", "this",
    "through", "together", "was", "were", "when", "with",
}


def music_brief(run_dir: Path, config: ProjectConfig) -> None:
    story = _story(run_dir)
    rendered = render_prompt(
        config,
        "music_brief",
        story=story,
        direction=config.music.direction,
    )
    words = [word.lower() for word in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", story)]
    terms = [word for word, _ in Counter(word for word in words if word not in _STOP_WORDS).most_common(8)]
    if not terms:
        raise StageExecutionError("music brief requires meaningful authored story terms")
    if config.mode == "real":
        brief = _llm(config, rendered).strip()
    else:
        brief = f"{config.music.direction}. Story motifs: {', '.join(terms)}. Instrumental underscore; support narration."
    atomic_write_json(
        run_dir / "music" / "brief.json",
        {
            "brief": brief,
            "configured_direction": config.music.direction,
            "story_terms": terms,
        },
    )


def _wav_duration(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as source:
            return source.getnframes() / source.getframerate()
    except (OSError, wave.Error, ZeroDivisionError) as wave_exc:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_name:format=format_name,duration",
                "-of", "json", str(path),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        duration = 0.0
        try:
            probe = json.loads(result.stdout)
            stream = probe["streams"][0]
            format_data = probe["format"]
            duration = float(format_data["duration"])
            valid = (
                result.returncode == 0
                and str(stream["codec_name"]).startswith("pcm_")
                and "wav" in str(format_data["format_name"]).split(",")
                and duration > 0
            )
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
            valid = False
        if valid:
            return duration
        raise StageExecutionError(f"invalid WAV artifact {path}: {wave_exc}") from wave_exc


def _first_wav_signal(path: Path, *, threshold_dbfs: float = -35.0) -> float:
    """Return the first PCM16 sample above a fixed, documented signal floor."""

    threshold = round(32767 * 10 ** (threshold_dbfs / 20))
    try:
        with wave.open(str(path), "rb") as source:
            if source.getsampwidth() != 2 or source.getcomptype() != "NONE":
                raise StageExecutionError(f"speech-onset QA requires PCM16 WAV: {path}")
            rate = source.getframerate()
            channels = source.getnchannels()
            samples = array("h")
            samples.frombytes(source.readframes(source.getnframes()))
    except (OSError, wave.Error, ZeroDivisionError) as exc:
        raise StageExecutionError(f"cannot measure speech onset in {path}: {exc}") from exc
    for sample_index in range(0, len(samples), channels):
        if max(abs(samples[sample_index + channel]) for channel in range(channels)) >= threshold:
            return sample_index / channels / rate
    raise StageExecutionError(f"no signal above {threshold_dbfs:g} dBFS in {path}")


def _media_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        text=True,
        capture_output=True,
        check=False,
    )
    try:
        duration = float(result.stdout.strip())
    except ValueError as exc:
        raise StageExecutionError(f"cannot probe media duration for {path}") from exc
    if result.returncode != 0 or duration <= 0:
        raise StageExecutionError(f"cannot probe media duration for {path}")
    return duration


def _write_tone(path: Path, duration: float, *, frequency: float, amplitude: int, sample_rate: int = 16_000) -> None:
    frame_count = max(1, int(duration * sample_rate))
    samples = array(
        "h",
        (
            int(amplitude * math.sin(2.0 * math.pi * frequency * index / sample_rate))
            for index in range(frame_count)
        ),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with wave.open(str(temporary), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(samples.tobytes())
    os.replace(temporary, path)


def tts(run_dir: Path, config: ProjectConfig) -> None:
    paced = (run_dir / "pacing" / "paced.txt").read_text(encoding="utf-8")
    output = run_dir / "audio" / "narration.wav"
    if config.mode == "synthetic":
        plan = read_json(run_dir / "pacing" / "plan.json")
        duration = max(1.0, min(float(plan["estimated_duration_seconds"]), 30.0))
        _write_tone(output, duration, frequency=220.0, amplitude=350)
        return
    tools = _section(config, "tools")
    generate_chatterbox_narration(
        paced,
        config.voice.reference_audio,
        output,
        exaggeration=config.voice.exaggeration,
        cfg_weight=config.voice.cfg_weight,
        temperature=config.voice.temperature,
        tempo=config.voice.tempo,
        ffmpeg=str(tools.get("ffmpeg", "ffmpeg")),
    )


def music(run_dir: Path, config: ProjectConfig) -> None:
    brief = read_json(run_dir / "music" / "brief.json")["brief"]
    duration = _wav_duration(run_dir / "audio" / "narration.wav")
    output = run_dir / "music" / "music.wav"
    if config.mode == "synthetic":
        _write_tone(output, duration, frequency=330.0, amplitude=180)
        return
    generate_musicgen_track(
        brief,
        duration,
        output,
        model_name=config.music.model,
        segment_duration=config.music.segment_duration,
        overlap=config.music.overlap,
    )


def _read_pcm16_mono(path: Path) -> tuple[int, array]:
    with wave.open(str(path), "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2 or source.getcomptype() != "NONE":
            raise StageExecutionError(f"synthetic mixer requires mono PCM16 WAV: {path}")
        rate = source.getframerate()
        samples = array("h")
        samples.frombytes(source.readframes(source.getnframes()))
        return rate, samples


def mix(run_dir: Path, config: ProjectConfig) -> None:
    narration = run_dir / "audio" / "narration.wav"
    bed = run_dir / "music" / "music.wav"
    output = run_dir / "audio" / "mix.wav"
    mix_config = _section(config, "mix")
    volume = mix_config.get("music_volume", 0.18)
    if isinstance(volume, bool) or not isinstance(volume, (int, float)) or not 0 <= volume <= 1:
        raise StageExecutionError("mix.music_volume must be between 0 and 1")
    if config.mode == "real":
        ffmpeg_mix(
            narration,
            bed,
            output,
            volume=float(volume),
            ffmpeg=str(_section(config, "tools").get("ffmpeg", "ffmpeg")),
        )
        return
    narration_rate, narration_samples = _read_pcm16_mono(narration)
    music_rate, music_samples = _read_pcm16_mono(bed)
    if narration_rate != music_rate:
        raise StageExecutionError("synthetic mixer inputs must have the same sample rate")
    mixed = array(
        "h",
        (
            max(-32768, min(32767, sample + int(music_samples[index] * float(volume))))
            for index, sample in enumerate(narration_samples)
        ),
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp")
    with wave.open(str(temporary), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(narration_rate)
        target.writeframes(mixed.tobytes())
    os.replace(temporary, output)


def _png(width: int, height: int, color: tuple[int, int, int]) -> bytes:
    signature = b"\x89PNG\r\n\x1a\n"

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(color) * width for _ in range(height))
    return signature + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def _image_checkpoint(run_dir: Path, scenes: list[dict], status: str) -> None:
    completed = []
    hashes = {}
    for scene in scenes:
        relative = scene["image"]
        path = run_dir / relative
        if path.is_file():
            completed.append(relative)
            hashes[relative] = sha256_file(path)
    atomic_write_json(
        run_dir / "staging" / "checkpoints" / "images.json",
        {
            "status": status,
            "expected": [scene["image"] for scene in scenes],
            "completed": completed,
            "hashes": hashes,
        },
    )


def images(run_dir: Path, config: ProjectConfig) -> None:
    scenes = read_json(run_dir / "visuals" / "scenes.json")["scenes"]
    _image_checkpoint(run_dir, scenes, "IN_PROGRESS")
    client = None
    workflow_template = None
    if config.mode == "real":
        comfy = _section(config, "comfyui")
        api_url = comfy.get("api_url")
        workflow_value = comfy.get("workflow_template")
        if not isinstance(api_url, str) or not api_url or not isinstance(workflow_value, str) or not workflow_value:
            raise StageExecutionError("comfyui.api_url and comfyui.workflow_template are required in real mode")
        workflow_path = (config.path.parent / workflow_value).resolve()
        workflow_template = read_json(workflow_path)
        client = ComfyUIClient(
            api_url,
            str(comfy.get("start_command", "")),
            str(comfy.get("stop_command", "")),
            int(comfy.get("timeout_seconds", 300)),
        )
        client.start()
    try:
        for scene in scenes:
            output = run_dir / scene["image"]
            if output.is_file():
                continue
            if config.mode == "synthetic":
                digest = hashlib.sha256(scene["prompt"].encode("utf-8")).digest()
                atomic_write_bytes(output, _png(64, 36, (digest[0], digest[1], digest[2])))
            else:
                assert client is not None and workflow_template is not None
                workflow = client.render_workflow(workflow_template, scene["prompt"], output.stem)
                client.generate(workflow, output)
            _image_checkpoint(run_dir, scenes, "IN_PROGRESS")
    finally:
        if client is not None:
            client.stop()
    _image_checkpoint(run_dir, scenes, "COMPLETED")
    entries = [
        {
            "path": scene["image"],
            "sha256": sha256_file(run_dir / scene["image"]),
            "duration_seconds": scene["duration_seconds"],
        }
        for scene in scenes
    ]
    atomic_write_json(run_dir / "images" / "index.json", {"images": entries})


def _srt_time(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    whole_seconds, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d},{milliseconds:03d}"


def captions(run_dir: Path, config: ProjectConfig) -> None:
    output = run_dir / "captions" / "raw.srt"
    if config.mode == "real":
        captions_config = _section(config, "captions")
        run_whisperx(
            run_dir / "audio" / "mix.wav",
            output,
            command=str(captions_config.get("command", "whisperx")),
            model=str(captions_config.get("model", "large-v3")),
            language=str(captions_config.get("language", "en")),
        )
        return
    plan = read_json(run_dir / "pacing" / "plan.json")
    blocks = []
    for index, chunk in enumerate(plan["chunks"], 1):
        start = float(chunk["start_seconds"])
        end = start + float(chunk["duration_seconds"])
        blocks.append(f"{index}\n{_srt_time(start)} --> {_srt_time(end)}\n{chunk['text']}")
    atomic_write_text(output, "\n\n".join(blocks) + "\n")


_SRT_BLOCK = re.compile(
    r"^\s*\d+\s*\n(\d{2}:\d{2}:\d{2},\d{3})\s+-->\s+(\d{2}:\d{2}:\d{2},\d{3})\s*\n(.+?)\s*$",
    re.DOTALL,
)


def normalize_srt(text: str) -> str:
    blocks = []
    for raw in re.split(r"\n\s*\n", text.strip()):
        match = _SRT_BLOCK.match(raw)
        if not match:
            raise StageExecutionError("caption postprocess received malformed SRT")
        start, end, caption = match.groups()
        caption = " ".join(line.strip() for line in caption.splitlines() if line.strip())
        blocks.append((start, end, caption))
    return "\n\n".join(f"{index}\n{start} --> {end}\n{caption}" for index, (start, end, caption) in enumerate(blocks, 1)) + "\n"


def _srt_seconds(value: str) -> float:
    hours, minutes, rest = value.split(":")
    seconds, milliseconds = rest.split(",")
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(milliseconds) / 1000


def _last_caption_end(path: Path) -> float:
    ends = re.findall(r"-->\s*(\d{2}:\d{2}:\d{2},\d{3})", path.read_text(encoding="utf-8"))
    if not ends:
        raise StageExecutionError("final captions contain no timed cues")
    return _srt_seconds(ends[-1])


def _first_caption_start(path: Path) -> float:
    starts = re.findall(r"(\d{2}:\d{2}:\d{2},\d{3})\s*-->", path.read_text(encoding="utf-8"))
    if not starts:
        raise StageExecutionError("final captions contain no timed cues")
    return _srt_seconds(starts[0])


def caption_postprocess(run_dir: Path, config: ProjectConfig) -> None:
    del config
    raw = (run_dir / "captions" / "raw.srt").read_text(encoding="utf-8")
    atomic_write_text(run_dir / "captions" / "final.srt", normalize_srt(raw))


def video(run_dir: Path, config: ProjectConfig) -> None:
    index = read_json(run_dir / "images" / "index.json")
    output = run_dir / "video" / "video.mp4"
    if config.mode == "synthetic":
        payload = {
            "notice": "provider-free synthetic placeholder; not playable production media",
            "audio_sha256": sha256_file(run_dir / "audio" / "mix.wav"),
            "captions_sha256": sha256_file(run_dir / "captions" / "final.srt"),
            "images": index["images"],
        }
        atomic_write_bytes(output, b"SYNTHETIC-MEDIA\n" + json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n")
        return
    video_config = _section(config, "video")
    scenes = [
        {**item, "absolute_image": str((run_dir / item["path"]).resolve())}
        for item in index["images"]
    ]
    content_duration = max(
        _wav_duration(run_dir / "audio" / "mix.wav"),
        _last_caption_end(run_dir / "captions" / "final.srt"),
    ) + 0.25
    ffmpeg_assemble_video(
        scenes,
        run_dir / "audio" / "mix.wav",
        run_dir / "captions" / "final.srt",
        output,
        width=int(video_config.get("width", 1920)),
        height=int(video_config.get("height", 1080)),
        fps=int(video_config.get("fps", 24)),
        crf=int(video_config.get("crf", 18)),
        content_duration_seconds=content_duration,
        ffmpeg=str(_section(config, "tools").get("ffmpeg", "ffmpeg")),
    )


def thumbnail(run_dir: Path, config: ProjectConfig) -> None:
    story = _story(run_dir)
    request = render_prompt(config, "thumbnail", story=story)
    output = run_dir / "thumbnail" / "thumbnail.png"
    if config.mode == "synthetic":
        digest = hashlib.sha256((config.project_name + request).encode("utf-8")).digest()
        atomic_write_bytes(output, _png(320, 180, (digest[0], digest[1], digest[2])))
        return
    first = read_json(run_dir / "images" / "index.json")["images"][0]["path"]
    thumb = _section(config, "thumbnail")
    ffmpeg_thumbnail(
        run_dir / first,
        output,
        width=int(thumb.get("width", 1280)),
        height=int(thumb.get("height", 720)),
        ffmpeg=str(_section(config, "tools").get("ffmpeg", "ffmpeg")),
    )


def _audited_paths(run_dir: Path) -> list[str]:
    fixed = [
        "authored/story.txt",
        "authored/checks.json",
        "pacing/plan.json",
        "visuals/scenes.json",
        "audio/narration.wav",
        "music/brief.json",
        "music/music.wav",
        "audio/mix.wav",
        "captions/final.srt",
        "video/video.mp4",
        "thumbnail/thumbnail.png",
    ]
    image_index = read_json(run_dir / "images" / "index.json")
    return sorted(set(fixed + [item["path"] for item in image_index["images"]]))


def _artifact_fingerprint(run_dir: Path) -> dict:
    artifacts = {relative: sha256_file(run_dir / relative) for relative in _audited_paths(run_dir)}
    aggregate = hashlib.sha256(json.dumps(artifacts, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"algorithm": "sha256", "aggregate": aggregate, "artifacts": artifacts}


def audit(run_dir: Path, config: ProjectConfig) -> None:
    failures = []
    checks = {}
    for relative in _audited_paths(run_dir):
        valid = (run_dir / relative).is_file() and (run_dir / relative).stat().st_size > 0
        checks[relative] = valid
        if not valid:
            failures.append(f"missing or empty artifact: {relative}")
    synthetic_marker = (run_dir / "video" / "video.mp4").read_bytes().startswith(b"SYNTHETIC-MEDIA\n")
    if config.mode == "synthetic" and not synthetic_marker:
        failures.append("synthetic video marker is missing")
    if config.mode == "real" and synthetic_marker:
        failures.append("real-mode audit found a synthetic video placeholder")
    for relative in ["thumbnail/thumbnail.png"] + [item["path"] for item in read_json(run_dir / "images" / "index.json")["images"]]:
        if not (run_dir / relative).read_bytes().startswith(b"\x89PNG\r\n\x1a\n"):
            failures.append(f"invalid PNG signature: {relative}")
    narration_duration = _wav_duration(run_dir / "audio" / "narration.wav")
    mix_duration = _wav_duration(run_dir / "audio" / "mix.wav")
    video_duration = _media_duration(run_dir / "video" / "video.mp4") if config.mode == "real" else mix_duration
    speech_onset = _first_wav_signal(run_dir / "audio" / "narration.wav") if config.mode == "real" else 0.0
    caption_start = _first_caption_start(run_dir / "captions" / "final.srt")
    caption_end = _last_caption_end(run_dir / "captions" / "final.srt")
    first_caption_delta = caption_start - speech_onset
    post_audio_tail = video_duration - mix_duration
    timing_checks = {
        "first_caption_within_0_5s_of_speech": abs(first_caption_delta) <= 0.5,
        "post_audio_tail_at_most_1s": -0.05 <= post_audio_tail <= 1.0,
        "last_caption_fits_video": caption_end <= video_duration + 0.05,
    }
    checks.update({f"timing.{name}": passed for name, passed in timing_checks.items()})
    for name, passed in timing_checks.items():
        if not passed:
            failures.append(f"timing check failed: {name}")
    report = {
        "status": "FAIL" if failures else "PASS",
        "synthetic": config.mode == "synthetic",
        "checks": checks,
        "timing": {
            "narration_duration_seconds": narration_duration,
            "mix_duration_seconds": mix_duration,
            "video_duration_seconds": video_duration,
            "speech_onset_seconds": speech_onset,
            "first_caption_seconds": caption_start,
            "first_caption_delta_seconds": first_caption_delta,
            "last_caption_seconds": caption_end,
            "post_audio_tail_seconds": post_audio_tail,
        },
        "failures": failures,
        "artifact_fingerprint": _artifact_fingerprint(run_dir),
    }
    atomic_write_json(run_dir / "audit" / "audit.json", report)
    if failures:
        raise StageExecutionError("deterministic audit failed: " + "; ".join(failures))


def finalize(run_dir: Path, config: ProjectConfig) -> None:
    report = read_json(run_dir / "audit" / "audit.json")
    if report.get("status") != "PASS":
        raise StageExecutionError("audit report is not PASS")
    current = _artifact_fingerprint(run_dir)
    if report.get("artifact_fingerprint") != current:
        raise StageExecutionError("audit fingerprint does not match current local artifacts")
    synthetic = bool(report.get("synthetic"))
    if synthetic != (config.mode == "synthetic"):
        raise StageExecutionError("current runtime mode does not match audited mode")
    atomic_write_json(
        run_dir / "package.json",
        {
            "status": "PASS",
            "media_kind": "synthetic-fake" if synthetic else "local-media",
            "notice": "Verified synthetic package; not a production video." if synthetic else "Verified local media package.",
            "audit": "audit/audit.json",
            "artifact_fingerprint": current,
        },
    )


HANDLERS: dict[str, Callable[[Path, ProjectConfig], None]] = {
    "authoring": authoring,
    "pacing": pacing,
    "scene_prompts": scene_prompts,
    "music_brief": music_brief,
    "tts": tts,
    "music": music,
    "mix": mix,
    "images": images,
    "captions": captions,
    "caption_postprocess": caption_postprocess,
    "video": video,
    "thumbnail": thumbnail,
    "audit": audit,
    "finalize": finalize,
}
