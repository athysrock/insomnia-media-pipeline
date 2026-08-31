"""Direct media/provider mechanics with no provider abstraction layer.

Imports for optional GPU tools stay inside the functions that need them, so
preflight, dry-run, and synthetic runs remain provider-free.
"""

from __future__ import annotations

import json
import math
import os
import shlex
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any


class MediaError(RuntimeError):
    pass


def chatterbox_controls(*, exaggeration: float, cfg_weight: float, temperature: float) -> dict[str, float]:
    """Return only controls passed natively to Chatterbox generation."""

    return {
        "exaggeration": float(exaggeration),
        "cfg_weight": float(cfg_weight),
        "temperature": float(temperature),
    }


def atempo_filter(tempo: float) -> str:
    """Build chained pitch-preserving ffmpeg atempo filters."""

    if tempo <= 0:
        raise MediaError("tempo must be positive")
    remaining = float(tempo)
    factors: list[float] = []
    while remaining > 2.0 + 1e-9:
        factors.append(2.0)
        remaining /= 2.0
    while remaining < 0.5 - 1e-9:
        factors.append(0.5)
        remaining /= 0.5
    if not factors or abs(remaining - 1.0) > 1e-9:
        factors.append(remaining)
    return ",".join(f"atempo={factor:g}" for factor in factors)


def musicgen_segment_plan(total_duration: int | float, segment_duration: int, overlap: int) -> list[int]:
    if total_duration <= 0 or segment_duration <= 0 or overlap < 0 or overlap >= segment_duration:
        raise MediaError("invalid MusicGen duration/overlap values")
    remaining = float(total_duration)
    plan = [min(segment_duration, math.ceil(remaining))]
    remaining -= plan[0]
    while remaining > 0:
        new_audio = min(segment_duration - overlap, math.ceil(remaining))
        plan.append(new_audio)
        remaining -= new_audio
    return plan


def _run(command: list[str], *, timeout: int, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaError(f"command failed to start or timed out: {command[0]}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-1000:]
        raise MediaError(f"command failed with exit {result.returncode}: {command[0]}: {detail}")
    return result


def run_llm_command(command: str, prompt: str, timeout: int) -> str:
    argv = shlex.split(command)
    if not argv:
        raise MediaError("llm.command must not be empty in real mode")
    output = _run(argv, timeout=timeout, input_text=prompt).stdout.strip()
    if not output:
        raise MediaError("llm.command returned empty output")
    return output


def generate_chatterbox_narration(
    text: str,
    reference_audio: Path,
    output: Path,
    *,
    exaggeration: float,
    cfg_weight: float,
    temperature: float,
    tempo: float,
    ffmpeg: str = "ffmpeg",
) -> None:
    try:
        import torch  # type: ignore
        import torchaudio  # type: ignore
        from chatterbox.tts import ChatterboxTTS  # type: ignore
    except ImportError as exc:
        raise MediaError("real TTS requires torch, torchaudio, and chatterbox") from exc
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = ChatterboxTTS.from_pretrained(device=device)
    chunks = [part.strip() for part in text.split("\n\n") if part.strip()]
    if not chunks:
        raise MediaError("paced narration is empty")
    controls = chatterbox_controls(
        exaggeration=exaggeration,
        cfg_weight=cfg_weight,
        temperature=temperature,
    )
    generated = [
        model.generate(chunk, audio_prompt_path=str(reference_audio), **controls)
        for chunk in chunks
    ]
    waveform = torch.cat(generated, dim=-1)
    output.parent.mkdir(parents=True, exist_ok=True)
    raw = output.with_suffix(".unpaced.wav")
    torchaudio.save(str(raw), waveform.cpu(), model.sr)
    if abs(tempo - 1.0) < 1e-9:
        os.replace(raw, output)
    else:
        _run(
            [ffmpeg, "-y", "-v", "error", "-i", str(raw), "-filter:a", atempo_filter(tempo), str(output)],
            timeout=1800,
        )
        raw.unlink(missing_ok=True)


def generate_musicgen_track(
    brief: str,
    total_duration: float,
    output: Path,
    *,
    model_name: str,
    segment_duration: int,
    overlap: int,
) -> None:
    """Generate a continued MusicGen track from the persisted dynamic brief."""

    try:
        import torch  # type: ignore
        import torchaudio  # type: ignore
        from audiocraft.models import MusicGen  # type: ignore
    except ImportError as exc:
        raise MediaError("real music generation requires torch, torchaudio, and audiocraft") from exc
    model = MusicGen.get_pretrained(model_name)
    model.set_generation_params(duration=segment_duration)
    sample_rate = model.sample_rate
    combined = None
    for index, _new_seconds in enumerate(musicgen_segment_plan(total_duration, segment_duration, overlap)):
        if index == 0:
            segment = model.generate([brief], progress=False)[0]
        else:
            overlap_samples = int(overlap * sample_rate)
            prompt_audio = combined[:, -overlap_samples:].unsqueeze(0)
            segment = model.generate_continuation(
                prompt_audio,
                prompt_sample_rate=sample_rate,
                descriptions=[brief],
                progress=False,
            )[0][:, overlap_samples:]
        combined = segment if combined is None else torch.cat([combined, segment], dim=-1)
    target_samples = int(total_duration * sample_rate)
    combined = combined[:, :target_samples]
    fade_samples = min(int(3 * sample_rate), combined.shape[-1])
    if fade_samples:
        combined[:, -fade_samples:] *= torch.linspace(1.0, 0.0, fade_samples, device=combined.device)
    output.parent.mkdir(parents=True, exist_ok=True)
    torchaudio.save(str(output), combined.cpu(), sample_rate)


def ffmpeg_mix(narration: Path, music: Path, output: Path, *, volume: float, ffmpeg: str = "ffmpeg") -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    filters = f"[1:a]volume={volume:g}[bed];[0:a][bed]amix=inputs=2:duration=first:normalize=0,loudnorm=I=-16:TP=-1.5:LRA=11[out]"
    _run(
        [ffmpeg, "-y", "-v", "error", "-i", str(narration), "-i", str(music), "-filter_complex", filters, "-map", "[out]", str(output)],
        timeout=600,
    )


def run_whisperx(
    audio: Path,
    output: Path,
    *,
    command: str = "whisperx",
    model: str = "large-v3",
    language: str = "en",
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    work = output.parent / ".whisperx"
    work.mkdir(parents=True, exist_ok=True)
    argv = shlex.split(command) + [
        str(audio), "--model", model, "--language", language, "--output_format", "srt", "--output_dir", str(work)
    ]
    _run(argv, timeout=1800)
    candidates = sorted(work.glob("*.srt"))
    if len(candidates) != 1:
        raise MediaError("WhisperX did not produce exactly one SRT file")
    os.replace(candidates[0], output)


def ffmpeg_assemble_video(
    scenes: list[dict[str, Any]],
    audio: Path,
    captions: Path,
    output: Path,
    *,
    width: int,
    height: int,
    fps: int,
    crf: int,
    content_duration_seconds: float | None = None,
    ffmpeg: str = "ffmpeg",
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    scene_total = sum(float(scene["duration_seconds"]) for scene in scenes)
    target_duration = scene_total if content_duration_seconds is None else float(content_duration_seconds)
    if not scenes or scene_total <= 0 or target_duration <= 0:
        raise MediaError("video scenes and content duration must be positive")
    duration_scale = target_duration / scene_total
    concat = output.parent / ".images.ffconcat"
    lines = ["ffconcat version 1.0"]
    for scene in scenes:
        image = Path(scene["absolute_image"])
        escaped = str(image).replace("'", "'\\''")
        lines.extend([f"file '{escaped}'", f"duration {float(scene['duration_seconds']) * duration_scale:.6f}"])
    # The concat demuxer applies the final duration only when a following file
    # supplies its endpoint. Repeating the last still is the documented form.
    final_image = Path(scenes[-1]["absolute_image"])
    final_escaped = str(final_image).replace("'", "'\\''")
    lines.append(f"file '{final_escaped}'")
    concat.write_text("\n".join(lines) + "\n", encoding="utf-8")
    subtitle_filter = str(captions).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    # Materialize frames before libass. Applying subtitles to sparse concat
    # packets and then using output -r duplicates an uncaptioned first still.
    vf = f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps={fps},subtitles='{subtitle_filter}'"
    _run(
        [ffmpeg, "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(concat), "-i", str(audio), "-vf", vf, "-t", f"{target_duration:.6f}", "-c:v", "libx264", "-crf", str(crf), "-pix_fmt", "yuv420p", "-c:a", "aac", str(output)],
        timeout=2400,
    )
    concat.unlink(missing_ok=True)


def ffmpeg_thumbnail(image: Path, output: Path, *, width: int, height: int, ffmpeg: str = "ffmpeg") -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    vf = f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"
    _run([ffmpeg, "-y", "-v", "error", "-i", str(image), "-vf", vf, str(output)], timeout=300)


class ComfyUIClient:
    """Minimal direct ComfyUI lifecycle and queue client."""

    def __init__(self, api_url: str, start_command: str = "", stop_command: str = "", timeout: int = 300):
        if urllib.parse.urlsplit(api_url).scheme not in {"http", "https"}:
            raise MediaError("ComfyUI API URL must use http or https")
        self.api_url = api_url.rstrip("/")
        self.start_command = start_command
        self.stop_command = stop_command
        self.timeout = timeout
        self.started_here = False
        self.process: Any | None = None

    def _json(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self.api_url + path,
            data=data,
            headers={"Content-Type": "application/json"} if data is not None else {},
        )
        # The constructor rejects every scheme except HTTP(S).
        with urllib.request.urlopen(request, timeout=10) as response:  # nosec B310
            return json.loads(response.read().decode("utf-8"))

    def healthy(self) -> bool:
        try:
            self._json("/system_stats")
            return True
        except (OSError, ValueError, urllib.error.URLError):
            return False

    def start(self) -> None:
        if self.healthy():
            return
        if not self.start_command:
            raise MediaError("ComfyUI is unavailable and comfyui.start_command is empty")
        self.process = subprocess.Popen(
            shlex.split(self.start_command), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        self.started_here = True
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            if self.healthy():
                return
            time.sleep(1)
        self.stop()
        raise MediaError("ComfyUI did not become healthy before timeout")

    def stop(self) -> None:
        if not self.started_here:
            return
        try:
            if self.stop_command:
                _run(shlex.split(self.stop_command), timeout=60)
        finally:
            process = self.process
            if process is not None:
                poll = getattr(process, "poll", None)
                if poll is None or poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
            self.process = None
            self.started_here = False

    @staticmethod
    def render_workflow(template: Any, prompt: str, prefix: str) -> Any:
        def substitute(value: Any) -> Any:
            if isinstance(value, str):
                return value.replace("{{PROMPT}}", prompt).replace("{{OUTPUT_PREFIX}}", prefix)
            if isinstance(value, list):
                return [substitute(item) for item in value]
            if isinstance(value, dict):
                return {key: substitute(item) for key, item in value.items()}
            return value

        return substitute(template)

    def generate(self, workflow: Any, output: Path) -> None:
        prompt_id = self._json("/prompt", {"prompt": workflow, "client_id": str(uuid.uuid4())})["prompt_id"]
        deadline = time.monotonic() + self.timeout
        image_info = None
        while time.monotonic() < deadline:
            history = self._json(f"/history/{prompt_id}").get(prompt_id)
            if history:
                for node in history.get("outputs", {}).values():
                    images = node.get("images", [])
                    if images:
                        image_info = images[0]
                        break
                if image_info:
                    break
            time.sleep(1)
        if not image_info:
            raise MediaError("ComfyUI did not return an image before timeout")
        query = urllib.parse.urlencode(image_info)
        # The constructor rejects every scheme except HTTP(S).
        with urllib.request.urlopen(  # nosec B310
            f"{self.api_url}/view?{query}", timeout=30
        ) as response:
            data = response.read()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(data)
