"""Conservative English narration sentence splitting for script and evidence checks."""
from __future__ import annotations

import re


_SENTENCE_END = re.compile(r".*?(?<!\d)[.!?]+(?=\s|$)|.+$", re.DOTALL)
_ABBREVIATIONS = re.compile(
    r"\b(?:U\.S\.|U\.K\.|U\.N\.|e\.g\.|i\.e\.|Mr\.|Mrs\.|Ms\.|Dr\.|Prof\.|St\.|vs\.)",
    re.IGNORECASE,
)
_SENTINEL = "\ue000"


def split_sentences(text: str) -> list[str]:
    """Split at sentence-ending punctuation without splitting decimal values."""
    value = str(text or "").strip()
    if not value:
        return []
    value = re.sub(r"(?<=\d)\.(?=\d)", _SENTINEL, value)
    value = _ABBREVIATIONS.sub(lambda match: match.group(0).replace(".", _SENTINEL), value)
    parts = [part.strip().replace(_SENTINEL, ".") for part in _SENTENCE_END.findall(value)]
    return [part for part in parts if part]
