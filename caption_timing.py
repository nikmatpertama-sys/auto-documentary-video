"""Natural phrase captions aligned to the actual Edge TTS word boundaries."""
from __future__ import annotations

import re
import unicodedata
from typing import Iterable

_WORD_RE = re.compile(r"[\w]+(?:['’][\w]+)*", re.UNICODE)
_TERMINAL = re.compile(r"[.!?…][\"'’”)]*$")
_CLAUSE = re.compile(r"[,;:][\"'’”)]*$")


def _norm(value: str) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).lower()
    return re.sub(r"[^a-z0-9]+", "", value)


def align_boundary_text(sentence: str, boundaries: list[dict]) -> list[dict]:
    """Restore original punctuation to Edge TTS word timing records.

    Edge TTS word-boundary text may omit punctuation. Aligns its tokens back
    to the original sentence, retaining apostrophes, commas and sentence ends.
    """
    matches = list(_WORD_RE.finditer(str(sentence or "")))
    source_tokens = []
    for index, match in enumerate(matches):
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(sentence)
        # The slice carries punctuation after this word, but not whitespace.
        display = sentence[match.start():next_start].strip()
        source_tokens.append({"norm": _norm(match.group(0)), "display": display})

    cleaned = []
    for item in boundaries:
        event_text = str(item.get("text", "")).strip()
        event_norm = _norm(event_text)
        if not event_norm:
            continue
        cleaned.append({**item, "_norm": event_norm})

    # Most Edge TTS responses have a 1:1 word count. If punctuation tokens
    # are emitted separately or a contraction differs, use ordered matching.
    if len(cleaned) == len(source_tokens) and all(
        _norm(e.get("text", "")) == t["norm"] or not _norm(e.get("text", ""))
        for e, t in zip(cleaned, source_tokens)
    ):
        pairs = list(zip(cleaned, source_tokens))
    else:
        pairs = []
        cursor = 0
        for event in cleaned:
            match_at = None
            for j in range(cursor, len(source_tokens)):
                if source_tokens[j]["norm"] == event["_norm"]:
                    match_at = j
                    break
            if match_at is not None:
                pairs.append((event, source_tokens[match_at]))
                cursor = match_at + 1
            # Do not guess when the TTS token does not match the script.
            # The completeness check below turns this into a clear failure
            # rather than attaching a valid timestamp to the wrong word.

    if len(pairs) != len(source_tokens):
        raise ValueError(
            "Edge TTS word-boundary count could not be aligned to every script word "
            f"({len(pairs)} aligned for {len(source_tokens)} spoken words)."
        )

    result = []
    for event, token in pairs:
        try:
            offset = max(0.0, float(event.get("offset", 0.0)))
            duration = max(0.0, float(event.get("duration", 0.0)))
        except (TypeError, ValueError):
            continue
        result.append({
            "text": token["display"],
            "offset": offset,
            "duration": duration,
        })
    if len(result) != len(source_tokens):
        raise ValueError(
            "At least one spoken word has no reliable subtitle timing; refusing to write incomplete captions."
        )
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

        # A comma/semicolon/colon is a useful breath point, but don't create
        # a one- or two-word flash caption unless the phrase is already long.
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
