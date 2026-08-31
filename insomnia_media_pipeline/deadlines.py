"""Single source of truth for fixed, unheartbeated Conductor stage budgets."""

from __future__ import annotations

from dataclasses import dataclass


REPORTING_MARGIN_SECONDS = 120
MAX_REAL_SCENES = 8
AUTHORING_CORRECTION_CALLS = 0
MAX_LLM_TIMEOUT_SECONDS = 900
MAX_COMFYUI_TIMEOUT_SECONDS = 540

# ComfyUI may spend up to 10 seconds beyond each polling deadline in an HTTP
# request, 30 seconds downloading each generated image, and 80 seconds stopping
# an owned process. Startup contributes one additional 10-second health probe.
COMFYUI_FIXED_OVERHEAD_SECONDS = MAX_REAL_SCENES * (10 + 30) + 80 + 10

STAGE_TIMEOUTS = {
    "init": 300,
    "authoring": 1800,
    "pacing": 1200,
    "scene_prompts": 8400,
    "tts": 2400,
    "music_brief": 1020,
    "music": 2400,
    "mix": 600,
    "images": 5400,
    "captions": 1800,
    "caption_postprocess": 300,
    "video": 2400,
    "thumbnail": 600,
    "audit": 600,
    "finalize": 300,
}


@dataclass(frozen=True)
class StageBudget:
    provider_calls: int
    provider_seconds: int
    fixed_overhead_seconds: int
    reporting_margin_seconds: int
    deadline_seconds: int

    @property
    def worst_case_seconds(self) -> int:
        return self.provider_calls * self.provider_seconds + self.fixed_overhead_seconds + self.reporting_margin_seconds


def real_stage_budgets(*, llm_timeout: int, comfyui_timeout: int) -> dict[str, StageBudget]:
    """Return worst-case configurable-provider work for one zero-retry attempt."""

    llm_calls = {
        "authoring": 1 + AUTHORING_CORRECTION_CALLS,
        "pacing": 1,
        "scene_prompts": 1 + MAX_REAL_SCENES,
        "music_brief": 1,
    }
    budgets = {
        stage: StageBudget(calls, llm_timeout, 0, REPORTING_MARGIN_SECONDS, STAGE_TIMEOUTS[stage])
        for stage, calls in llm_calls.items()
    }
    budgets["images"] = StageBudget(
        1 + MAX_REAL_SCENES,
        comfyui_timeout,
        COMFYUI_FIXED_OVERHEAD_SECONDS,
        REPORTING_MARGIN_SECONDS,
        STAGE_TIMEOUTS["images"],
    )
    return budgets
