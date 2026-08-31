---
name: insomnia-media-pipeline
description: Run one supplied story to a verified local package.
version: 0.1.0
author: athysrock
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [media, pipeline, conductor, local-artifacts]
    related_skills: []
---

# Run One Story Project

Keep the caller's story and project config as the only creative inputs. Never discover, select, queue, or substitute a different story. Never perform a remote destination action.

## Clarify tone

Ask only questions that materially affect the existing story:

- Confirm the intended mood, energy curve, narration expression, visual style, and ending feel.
- Confirm the music direction in plain creative language.
- Distinguish prose or pacing guidance from speech-model controls. Treat only `exaggeration`, `cfg_weight`, and `temperature` as native Chatterbox controls; treat `tempo` as pitch-preserving ffmpeg postprocessing.

Translate the answers conservatively into supported dials:

| Plain-language delivery | Supported adjustment |
|---|---|
| Upbeat, animated, higher energy | Raise `exaggeration` moderately; use a slightly higher `temperature` only when more variation is wanted. |
| Downbeat, restrained, lower energy | Lower `exaggeration`; keep `temperature` moderate for stable delivery. |
| Faster or slower pace | Raise or lower `tempo`; this uses ffmpeg `atempo` after synthesis and preserves pitch. |
| Tighter or looser model guidance/prosody | Raise or lower `cfg_weight` within the validated range. |

There is no pitch control in the implemented Chatterbox path. Do not invent one or claim that tone prose is passed to the speech model. Tone/style is approximated only through the supported energy, variation, guidance, and speed dials above; confirm the result by listening during a real run.

Apply agreed changes to the caller's config or external templates. Keep `voice.reference_audio`; do not invent a fallback voice.

When no delivery override is requested, omit all four delivery keys and use the project defaults: `exaggeration: 0.5`, `cfg_weight: 0.5`, `temperature: 0.8`, and `tempo: 0.9`. Per-project overrides remain supported when explicitly wanted.

## Validate before launch

From the repository root, run:

```bash
python3 -m insomnia_media_pipeline preflight --story STORY --config CONFIG
python3 -m insomnia_media_pipeline dry-run --story STORY --config CONFIG --run-dir RUN_DIR
```

Require both reports to say `PASS`. Resolve the voice reference and prompt directory relative to the config. Check that all seven prompt files remain external and valid: authoring, authoring checks, pacing, scene selection, scene rendering, thumbnail, and music brief.

Remember that config and prompt templates are live reads. They are not copied into a run and do not invalidate completed receipts. If they change mid-run, explain that completed and later stages can reflect different revisions; start a fresh run when consistency is required.

## Launch and monitor

Use the generated Conductor definition for durable real-media execution. Let Conductor own retries and recovery. Provider-specific workers may use separate Python environments. On an 8 GB GPU, the TTS process must exit before the MusicGen process starts. The general worker excludes both GPU tasks. Wait for Conductor to schedule TTS, then run its worker once in the foreground so it reports completion before exiting. Start MusicGen only after that process has terminated and the atomic TTS receipt exists:

```bash
python3 conductor/definitions/generate_definitions.py
python3 -m conductor.client register --url CONDUCTOR_URL
python3 -m conductor.worker --url CONDUCTOR_URL \
  --exclude-task insomnia_media_pipeline_tts \
  --exclude-task insomnia_media_pipeline_music &
WORKFLOW_ID=$(python3 -m conductor.client launch --url CONDUCTOR_URL \
  --story STORY --config CONFIG --run-dir RUN_DIR | \
  python3 -c 'import json,sys; print(json.load(sys.stdin)["workflow_id"])')
until python3 -m conductor.client monitor --url CONDUCTOR_URL --workflow-id "$WORKFLOW_ID" | \
  python3 -c 'import json,sys; d=json.load(sys.stdin); raise SystemExit(not any(t.get("taskDefName") == "insomnia_media_pipeline_tts" and t.get("status") == "SCHEDULED" for t in d.get("tasks", [])))'
do sleep 2; done
/path/to/tts/bin/python -m conductor.worker --url CONDUCTOR_URL \
  --task insomnia_media_pipeline_tts --once
test -f RUN_DIR/receipts/tts.json
/path/to/musicgen/bin/python -m conductor.worker --url CONDUCTOR_URL \
  --task insomnia_media_pipeline_music
```

When a provider stage fails after Conductor has durably accepted prior stages, release the conflicting GPU process and use Conductor's workflow `retry` operation to retry the failed stage. Do not use workflow `restart`: this restarts `init`, which correctly rejects the non-empty retained run directory.

Keep the returned workflow identifier. Monitor durable state with:

```bash
python3 -m conductor.client monitor --url CONDUCTOR_URL --workflow-id WORKFLOW_ID
```

Use `synthetic-run` only for the provider-free fake-media check:

```bash
python3 -m insomnia_media_pipeline synthetic-run --story STORY --config CONFIG --run-dir RUN_DIR
```

Verify local artifact state without mutating work:

```bash
python3 -m insomnia_media_pipeline status --run-dir RUN_DIR
```

When durable orchestration retries a task, invoke the named stage through the Conductor worker. For a local interrupted synthetic check, use `resume --run-dir RUN_DIR`. Do not pass a mutable stage object between workers; reconstruct from `run.json` and artifacts.

## Verify local completion

Accept completion only when all 15 stages are complete, `audit/audit.json` says `PASS`, `package.json` says `PASS`, and the package fingerprint matches current files. For real media, independently verify the burned first caption against measured narration onset and require the post-audio tail to be at most one second; do not infer either from SRT timestamps alone. Treat `synthetic-fake` as contract evidence only, never as a real generated video. Report the local run directory, audit path, package path, media kind, and any reused stages.
