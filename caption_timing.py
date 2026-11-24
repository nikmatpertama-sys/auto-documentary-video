"""Natural phrase captions aligned to actual Edge TTS word boundaries.

Missing boundary events are estimated locally from adjacent reliable timings and
sentence audio duration. The original spoken words are never dropped merely
because Edge TTS omitted a WordBoundary event.
"""
from __future__ import annotations

import math
import re
import statistics
import unicodedata
from typing import Optional

_WORD_RE = re.compile(r"[\w]+(?:['’][\w]+)*", re.UNICODE)
_TERMINAL = re.compile(r"[.!?…][\"'’”)]*$")
_CLAUSE = re.compile(r"[,;:][\"'’”)]*$")


def _norm(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).lower()
    return re.sub(r"[^a-z0-9]+", "", value)


def _syllable_weight(value: str) -> float:
    """Small language-neutral-ish duration weight for an English TTS token."""
    word = _norm(value)
    if not word:
        return 1.0
    groups = re.findall(r"[aeiouy]+", word)
    count = max(1, len(groups))
    if len(word) > 7:
        count = max(count, 3)
    return float(min(5, count))


def _lcs_pairs(source_norms: list[str], event_norms: list[str]) -> list[tuple[int, int]]:
    """Ordered exact token matching; handles omitted and extra boundary events."""
    n, m = len(source_norms), len(event_norms)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            if source_norms[i] == event_norms[j]:
                dp[i][j] = 1 + dp[i + 1][j + 1]
            else:
                dp[i][j] = max(dp[i + 1][j], dp[i][j + 1])

    pairs: list[tuple[int, int]] = []
    i = j = 0
    while i < n and j < m:
        if source_norms[i] == event_norms[j]:
            pairs.append((i, j))
            i += 1
            j += 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1
    return pairs


def _finite_nonnegative(value, default=0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.0, number) if math.isfinite(number) else default


def _estimate_block(
    tokens: list[dict],
    block_start: int,
    block_end: int,
    left_time: float,
    right_time: float,
    typical_per_syllable: float,
    anchor_right: bool,
) -> dict[int, tuple[float, float]]:
    """Estimate only missing words inside the timing gap between known anchors."""
    indices = list(range(block_start, block_end))
    if not indices:
        return {}

    left = max(0.0, float(left_time))
    right = max(left, float(right_time))
    available = right - left
    weights = [_syllable_weight(tokens[i]["raw"]) for i in indices]
    weight_total = max(1.0, sum(weights))

    desired = typical_per_syllable * weight_total
    # Never let estimates spill beyond the sentence audio or a neighbouring
    # reliable word. If the interval is tiny, the estimates share that interval.
    allocated = min(available, max(0.055 * len(indices), desired))
    if allocated <= 0.0:
        return {i: (left, 0.04) for i in indices}

    cursor = right - allocated if anchor_right else left
    result: dict[int, tuple[float, float]] = {}
    for index, weight in zip(indices, weights):
        duration = allocated * (weight / weight_total)
        result[index] = (cursor, duration)
        cursor += duration
    return result


def align_boundary_text(
    sentence: str,
    boundaries: list[dict],
    audio_duration: Optional[float] = None,
) -> list[dict]:
    """Restore punctuation and retain every source word in caption timing.

    Word boundaries supplied by Edge TTS are marked ``edge_tts``. If an event
    is missing, its word is estimated between the nearest reliable event(s),
    weighted by syllable count. When the provider returns no usable boundaries,
    the complete sentence is timed across its measured audio duration.
    """
    sentence = str(sentence or "")
    matches = list(_WORD_RE.finditer(sentence))
    if not matches:
        return []

    source_tokens = []
    for index, match in enumerate(matches):
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(sentence)
        source_tokens.append({
            "norm": _norm(match.group(0)),
            "raw": match.group(0),
            "display": sentence[match.start():next_start].strip(),
        })

    cleaned = []
    for item in boundaries or []:
        event_text = str(item.get("text", "")).strip()
        event_norm = _norm(event_text)
        if not event_norm:
            continue
        try:
            offset = float(item.get("offset", 0.0))
            duration = float(item.get("duration", 0.0))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(offset) or not math.isfinite(duration):
            continue
        cleaned.append({
            "text": event_text,
            "norm": event_norm,
            "offset": max(0.0, offset),
            "duration": max(0.0, duration),
        })

    measured_duration = _finite_nonnegative(audio_duration, 0.0)
    last_event_end = max(
        (item["offset"] + item["duration"] for item in cleaned),
        default=0.0,
    )
    if measured_duration <= 0.0:
        # Backwards-compatible default for callers without an audio probe.
        # Main production always supplies ffprobe's measured sentence length.
        median_duration = statistics.median(
            [x["duration"] for x in cleaned if x["duration"] > 0.02] or [0.20]
        )
        missing_guess = max(0, len(matches) - len(cleaned))
        measured_duration = max(last_event_end, 0.0) + median_duration * missing_guess
    else:
        # The measured audio file is authoritative. Discard out-of-range
        # events and let the estimator restore their script words safely.
        bounded = []
        for event in cleaned:
            if event["offset"] >= measured_duration:
                continue
            event = dict(event)
            event["duration"] = min(event["duration"], measured_duration - event["offset"])
            if event["duration"] > 0.01:
                bounded.append(event)
        cleaned = bounded
    pairs = _lcs_pairs(
        [token["norm"] for token in source_tokens],
        [event["norm"] for event in cleaned],
    )

    matched_durations = []
    for source_index, event_index in pairs:
        event = cleaned[event_index]
        if event["duration"] > 0.025:
            matched_durations.append(
                event["duration"] / _syllable_weight(source_tokens[source_index]["raw"])
            )
    if matched_durations:
        typical_per_syllable = statistics.median(matched_durations)
    else:
        total_weight = sum(_syllable_weight(x["raw"]) for x in source_tokens)
        typical_per_syllable = measured_duration / max(1.0, total_weight)
    typical_per_syllable = min(0.65, max(0.055, typical_per_syllable))

    timed: dict[int, dict] = {}
    for source_index, event_index in pairs:
        event = cleaned[event_index]
        start = min(measured_duration, max(0.0, event["offset"]))
        duration = min(max(0.035, event["duration"]), max(0.035, measured_duration - start))
        timed[source_index] = {
            "offset": start,
            "duration": duration,
            "timing_source": "edge_tts",
        }

    # Walk missing-token runs bounded by the previous and next reliable events.
    cursor = 0
    matched_indices = sorted(timed)
    for matched_index in matched_indices + [len(source_tokens)]:
        if matched_index > cursor:
            previous = cursor - 1
            following = matched_index
            left_time = (
                timed[previous]["offset"] + timed[previous]["duration"]
                if previous in timed else 0.0
            )
            right_time = (
                timed[following]["offset"]
                if following in timed else measured_duration
            )
            # Leading missing words sit immediately before the first reliable
            # anchor; internal/trailing missing words follow the previous word.
            estimates = _estimate_block(
                source_tokens,
                cursor,
                matched_index,
                left_time,
                right_time,
                typical_per_syllable,
                anchor_right=(previous < 0 and following in timed),
            )
            for source_index, (offset, duration) in estimates.items():
                timed[source_index] = {
                    "offset": offset,
                    "duration": duration,
                    "timing_source": "estimated",
                }
        cursor = matched_index + 1

    # A provider can return timestamps that are internally inconsistent. If
    # that leaves a missing or overlapping sequence, rebuild timings uniformly
    # from the measured sentence duration instead of producing broken karaoke.
    ordered = [timed.get(i) for i in range(len(source_tokens))]
    invalid = any(item is None for item in ordered)
    if not invalid:
        previous_start = -1.0
        for item in ordered:
            if item["offset"] < previous_start - 0.01 or item["offset"] > measured_duration + 0.01:
                invalid = True
                break
            previous_start = item["offset"]

    if invalid:
        weights = [_syllable_weight(x["raw"]) for x in source_tokens]
        total_weight = sum(weights)
        cursor_time = 0.0
        ordered = []
        for weight in weights:
            duration = measured_duration * (weight / total_weight)
            ordered.append({
                "offset": cursor_time,
                "duration": duration,
                "timing_source": "estimated_full_sentence",
            })
            cursor_time += duration

    result = []
    for token, timing in zip(source_tokens, ordered):
        result.append({
            "text": token["display"],
            "offset": max(0.0, float(timing["offset"])),
            "duration": max(0.035, float(timing["duration"])),
            "timing_source": timing["timing_source"],
        })
    return result


def build_phrase_groups(word_events: list[dict], max_words: int = 7,
                        max_chars: int = 44, max_span: float = 2.55) -> list[list[dict]]:
    """Group words at natural clause/sentence boundaries, not fixed word counts."""
    groups: list[list[dict]] = []
    current: list[dict] = []

    def flush():
        nonlocal current
        if current:
            groups.append(current)
            current = []

    for word in word_events:
        token = str(word.get("text", "")).strip()
        if not token:
            continue
        try:
            start = float(word.get("offset", 0.0))
            duration = max(0.0, float(word.get("duration", 0.0)))
        except (TypeError, ValueError):
            continue

        if current:
            phrase = " ".join(x["text"] for x in current)
            proposed = f"{phrase} {token}"
            current_start = float(current[0]["offset"])
            current_end = max(
                float(x["offset"]) + float(x.get("duration", 0.0)) for x in current
            )
            previous_sentence = current[-1].get("sentence_index")
            current_sentence = word.get("sentence_index")
            should_break = (
                (previous_sentence is not None and current_sentence is not None
                 and previous_sentence != current_sentence)
                or _TERMINAL.search(current[-1]["text"]) is not None
                or len(current) >= max_words
                or len(proposed) > max_chars
                or start - current_start > max_span
                or start - current_end > 0.34
            )
            if should_break:
                flush()

        current.append({**word, "text": token, "offset": start, "duration": duration})

        if _CLAUSE.search(token) and len(current) >= 4:
            flush()
        elif _TERMINAL.search(token):
            flush()

    flush()
    return groups


def ass_escape(text: str) -> str:
    return str(text).replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")


def karaoke_phrase(group: list[dict]) -> str:
    """Create word-level ASS karaoke timing, including natural audio gaps."""
    if not group:
        return ""
    output = []
    cursor = float(group[0].get("offset", 0.0))
    for index, word in enumerate(group):
        start = max(cursor, float(word.get("offset", cursor)))
        gap = max(0.0, start - cursor)
        if gap >= 0.035 and index > 0:
            gap_cs = max(1, min(300, int(round(gap * 100))))
            output.append(f"{{\\k{gap_cs}}} ")
        duration = max(0.06, float(word.get("duration", 0.0)))
        duration_cs = max(1, min(300, int(round(duration * 100))))
        output.append(f"{{\\kf{duration_cs}}}{ass_escape(word['text'])}")
        cursor = start + duration
        if index + 1 < len(group):
            output.append(" ")
    return "".join(output)


def group_bounds(group: list[dict]) -> tuple[float, float]:
    start = float(group[0]["offset"])
    end = max(
        float(item["offset"]) + max(0.06, float(item.get("duration", 0.0)))
        for item in group
    )
    return start, max(start + 0.35, end)
