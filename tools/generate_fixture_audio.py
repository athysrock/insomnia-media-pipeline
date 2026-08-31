#!/usr/bin/env python3
"""Generate the deterministic, generic PCM voice-reference fixture."""

from __future__ import annotations

import argparse
import math
import wave
from array import array
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    sample_rate = 16_000
    samples = array(
        "h",
        (
            int(500 * math.sin(2 * math.pi * 180 * index / sample_rate))
            for index in range(sample_rate // 2)
        ),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(args.output), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(samples.tobytes())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
