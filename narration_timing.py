"""Small, testable helpers for keeping narration inside the video target."""

import math

MIN_DURATION_SECONDS = 70.0
MAX_DURATION_SECONDS = 90.0
LOW_TARGET_SECONDS = 71.0
HIGH_TARGET_SECONDS = 89.0
# FFmpeg's atempo preserves pitch, so a slightly wider floor is safe and stops
# a legitimate 150-160 word script (about 57-60s of speech) from failing the run
# with "aggressive tempo change" after the TTS stage has already been paid for.
MIN_SAFE_ATEMPO = 0.80
MAX_SAFE_ATEMPO = 1.15


def duration_correction(observed_seconds):
    """Return a pitch-preserving FFmpeg atempo correction, or None if in range.

    The target deliberately stays one second inside the hard duration limits
    to absorb tiny differences between FFmpeg builds and container metadata.
    """
    try:
        observed = float(observed_seconds)
    except (TypeError, ValueError) as exc:
        raise ValueError("Narration duration must be a number.") from exc

    if not math.isfinite(observed) or observed <= 0:
        raise ValueError("Narration duration must be a finite positive number.")

    if MIN_DURATION_SECONDS <= observed <= MAX_DURATION_SECONDS:
        return None

    target = LOW_TARGET_SECONDS if observed < MIN_DURATION_SECONDS else HIGH_TARGET_SECONDS
    atempo = observed / target
    if not MIN_SAFE_ATEMPO <= atempo <= MAX_SAFE_ATEMPO:
        raise ValueError(
            f"Narration duration {observed:.2f}s would need an aggressive tempo change "
            "outside the safe 0.85-1.15 range. Generate a better-length script instead."
        )

    return {"atempo": atempo, "target_duration": target}
