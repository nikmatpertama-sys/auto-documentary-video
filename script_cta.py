"""Closing call to action for every documentary narration.

The CTA is part of the spoken script, not metadata: it has to be the last
sentence the narration says, it must ask the viewer to follow the channel and
to comment, and it must be present in the generated voice-over.

Kept in its own module so the scriptwriter (``main.py``) and the final quality
control (``qc.py``) apply exactly the same rule.
"""

import hashlib
import re

FOLLOW_PATTERN = re.compile(
    r"\b(follow|follows|following|subscribe|subscribed|subscribing)\b", re.IGNORECASE
)
COMMENT_PATTERN = re.compile(
    r"\b(comment|comments|commented|reply|replies|let us know|tell us)\b",
    re.IGNORECASE,
)

CTA_TEMPLATES = {
    "en": [
        "Follow for more documentaries like this, and tell us in the comments what you would explore next.",
        "Follow to catch the next documentary, and let us know your thoughts in the comments below.",
        "Follow so you do not miss the next story, and share your questions in the comments.",
        "Follow for the next documentary in this series, and drop your thoughts in the comments below.",
        "Follow for more stories from this world, and tell us in the comments what we should cover next.",
    ],
    "id": [
        "Ikuti untuk dokumenter berikutnya, dan tulis pendapatmu di kolom komentar.",
        "Ikuti terus channel ini untuk dokumenter lain, dan bagikan pendapatmu di komentar.",
        "Ikuti untuk cerita berikutnya, dan beri tahu kami di kolom komentar apa yang ingin kamu jelajahi.",
        "Jangan lewatkan dokumenter berikutnya: ikuti channel ini dan tulis pertanyaanmu di komentar.",
    ],
}

DEFAULT_TEMPLATES = CTA_TEMPLATES["en"]


def split_sentences(text):
    return [
        item.strip()
        for item in re.findall(r"[^.!?]+[.!?]+|[^.!?]+$", str(text or ""))
        if item.strip()
    ]


def is_cta_sentence(sentence):
    """True only for a sentence that asks for both a follow and a comment."""
    text = str(sentence or "")
    return bool(FOLLOW_PATTERN.search(text) and COMMENT_PATTERN.search(text))


def ends_with_cta(script):
    sentences = split_sentences(script)
    return bool(sentences) and is_cta_sentence(sentences[-1])


def last_sentence(script):
    sentences = split_sentences(script)
    return sentences[-1] if sentences else ""


def build_cta(language, topic=None, seed=None):
    """Pick a natural, varied CTA in the narration's language.

    The choice is deterministic for one topic so a re-run does not swap the
    closing line under an already approved script.
    """
    code = str(language or "en").lower()
    templates = CTA_TEMPLATES.get(code)
    if not templates:
        raise RuntimeError(
            f"No closing CTA template exists for the narration language {code!r}. "
            "Add one to script_cta.py; speaking the CTA in another language is "
            "not an acceptable fallback."
        )
    key = f"{topic or ''}|{seed or ''}|{code}".encode("utf-8")
    index = int(hashlib.sha256(key).hexdigest()[:8], 16) % len(templates)
    return templates[index]


def strip_cta_sentences(text):
    """Remove CTA sentences from a narration, so only the final one is spoken."""
    sentences = split_sentences(text)
    kept = [sentence for sentence in sentences if not is_cta_sentence(sentence)]
    return " ".join(kept).strip()


def enforce_cta(narrations, language, topic=None, seed=None):
    """Guarantee the narration ends with exactly one follow+comment sentence.

    ``narrations`` is the per-scene narration text. A script that already ends
    with a CTA keeps it verbatim; otherwise any stray CTA is removed and a
    natural closing sentence is appended to the last scene.

    Returns ``(narrations, cta_info)``.
    """
    narrations = [str(item or "").strip() for item in narrations]
    if not narrations:
        return narrations, {"text": "", "source": "missing"}

    script = " ".join(narrations).strip()
    if ends_with_cta(script):
        return narrations, {"text": last_sentence(script), "source": "script"}

    cleaned = [strip_cta_sentences(item) for item in narrations]
    cleaned = [item for item in cleaned if item] or narrations
    cta = build_cta(language, topic=topic, seed=seed)
    cleaned[-1] = f"{cleaned[-1].rstrip()} {cta}".strip()
    return cleaned, {"text": cta, "source": "appended"}
