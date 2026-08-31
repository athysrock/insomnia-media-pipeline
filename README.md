# Insomnia Media Pipeline

A neutral, local, artifact-driven Python pipeline that turns one caller-supplied story and one YAML/JSON project config into an audited local package. Conductor owns durable sequencing; each worker invocation reconstructs context from files in the run directory.

The standard-library synthetic path is intentionally fake media. It exercises every contract without model downloads, service calls, or GPU work and labels its placeholder video so it cannot be mistaken for a production render.

## Install

```bash
python3 -m pip install .
```

The wheel contains both `insomnia_media_pipeline` and the required `conductor` client and worker modules.

## Quick check

```bash
python3 -m insomnia_media_pipeline preflight \
  --story fixtures/community-garden/story.txt \
  --config fixtures/community-garden/project.yaml

python3 -m insomnia_media_pipeline dry-run \
  --story fixtures/community-garden/story.txt \
  --config fixtures/community-garden/project.yaml \
  --run-dir runs/field-day

python3 -m insomnia_media_pipeline synthetic-run \
  --story fixtures/community-garden/story.txt \
  --config fixtures/community-garden/project.yaml \
  --run-dir runs/field-day

python3 -m insomnia_media_pipeline status --run-dir runs/field-day
```

`resume` invokes the artifact stages again in order; intact stages return `REUSED`. `stage --name NAME` is the stateless worker boundary used by Conductor.

## Live configuration contract

The run keeps the caller story as `input/story.txt` and stores only the external config path in `run.json`. It never copies the project config or prompt directory into the run, and config/prompt contents are excluded from receipt hashes and resume invalidation. Every stage execution reloads the config; every prompt-driven stage reads its template immediately before use.

This permits deliberate editing during a run, but it also permits a mixed result: completed stages keep the prior interpretation while later stages see newer config/templates. Resume does not invalidate completed work for those edits. Start a fresh run when one coherent config/template revision matters.

`voice.reference_audio` is mandatory and relative to the config directory. Preflight requires a readable PCM WAV between 0.1 and 300 seconds. There is no fallback voice. Delivery controls are optional and default to a stable, soothing profile: `exaggeration: 0.5`, `cfg_weight: 0.5`, `temperature: 0.8`, and pitch-preserving `tempo: 0.9`. Per-project values still override these defaults. The first three are the only speech-model generation values; `tempo` is implemented afterward with ffmpeg's `atempo` filter.

Video assembly derives its duration from the current mix and final caption end, adds a 0.25-second readability tail, and scales the existing scene allocation to that target. Captions are burned after the still-image sequence is materialized at the configured frame rate, so the first cue is not delayed until the first scene transition. The terminal audit rejects a first-caption/speech-onset delta over 0.5 seconds or an unexplained post-audio tail over 1.0 second.

## Real media mode

Set `runtime.mode` to `real` and provide the relevant optional sections shown by `compatibility.json`. The implementation calls a single configured language-model command, Chatterbox, MusicGen, ComfyUI, WhisperX, and ffmpeg directly. There is no provider/plugin framework. Preflight and dry-run do not invoke these providers. Direct CLI sequencing refuses real mode; register, run the worker, and launch through Conductor:

```bash
python3 conductor/definitions/generate_definitions.py
python3 -m conductor.client register
python3 -m conductor.worker
python3 -m conductor.client launch --story STORY --config CONFIG --run-dir RUN_DIR
python3 -m conductor.client monitor --workflow-id WORKFLOW_ID
```

The terminal `audit/audit.json` deterministically hashes local artifacts. `package.json` is created only when the current artifacts match a PASS audit. Nothing in this repository performs a remote destination action.

### 8 GB GPU worker sequence

TTS and MusicGen must not share the GPU at the same time. Use the fail-closed helper with the Python executable for each provider environment. It stops the retained general worker before GPU work, runs each GPU worker once, verifies `RUN_DIR/staging/receipts/tts.json` after the TTS process exits, and starts MusicGen only then. After the MusicGen process exits and its receipt exists, the helper restarts the general worker for image and caption work:

```bash
PYTHON=/path/to/general/bin/python \
TTS_PYTHON=/path/to/tts/bin/python \
MUSICGEN_PYTHON=/path/to/musicgen/bin/python \
CONDUCTOR_URL=http://localhost:8080 \
tools/run_8gb_sequence.sh STORY CONFIG RUN_DIR
```

The helper monitors the workflow to a terminal result and terminates the retained general-worker PID on success, provider failure, workflow failure, or interruption. Do not start a separate MusicGen worker early: a polling worker can claim the task before the TTS process releases GPU memory.

## License

The project is available under the MIT License. External models, services, and tools retain their own terms; see `NOTICE`.
