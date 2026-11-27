"""MM7: footage licence resolution and provenance evidence.

A search result's licence is only treated as permission to reuse when the
provider metadata names an explicit reusable licence. Strings such as
"See file page", "verify page terms" or an empty value are *unconfirmed*, not
permission. Licence validation is deliberately independent of visual
relevance: a visually perfect clip with an unconfirmed licence must still be
rejected when reuse confirmation is required.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

# Explicit free/reusable licences. Matching is on the normalised licence text,
# so provider-specific wording (Commons, Internet Archive, NASA, Coverr) all
# resolve to a confirmed status without guessing.
_REUSABLE_PATTERNS = (
    ("CC0", re.compile(r"\bcc[\s-]?0\b|\bcc0\b|creative commons zero", re.I)),
    ("Public domain", re.compile(r"public[\s-]?domain|\bpd\b|no known copyright", re.I)),
    ("Creative Commons Attribution", re.compile(r"cc[\s-]?by(?:\s|-)?(?!sa|nc|nd)\s*(?:[234]\.0)?|attribution\s*[234]\.0", re.I)),
    ("Creative Commons Attribution-ShareAlike", re.compile(r"cc[\s-]?by[\s-]?sa|attribution[\s-]?share[\s-]?alike", re.I)),
    ("GNU Free Documentation License", re.compile(r"\bgfdl\b|free documentation license", re.I)),
    ("Pixabay Content License", re.compile(r"pixabay content license", re.I)),
    ("Pexels License", re.compile(r"pexels license", re.I)),
    ("Coverr License", re.compile(r"coverr license", re.I)),
    ("NASA media usage guidelines", re.compile(r"nasa media usage guidelines", re.I)),
)

# Licences that are public but cannot be used in a monetised video, or that
# require permission. These are never auto-accepted.
_RESTRICTED_PATTERNS = (
    ("NonCommercial", re.compile(r"\bcc[\s-]?by[\s-]?nc\b|non[\s-]?commercial", re.I)),
    ("NoDerivatives", re.compile(r"\bcc[\s-]?by[\s-]?nd\b|no[\s-]?deriv", re.I)),
    ("All rights reserved", re.compile(r"all rights reserved|©|copyright(?:ed)?(?!\s+free)", re.I)),
)

# Text that promises nothing: it asks the user to go and check, so it is not a
# confirmed reuse grant.
_UNCONFIRMED_HINTS = re.compile(
    r"verify|see (?:the )?file page|check (?:the )?source|unknown|unspecified|"
    r"indicat(?:es|ed)|permission|contact (?:the )?(?:author|owner)|"
    r"usage (?:terms|rights) (?:apply|may)|licen[cs]e information (?:unavailable|missing)",
    re.I,
)

_ATTRIBUTION_REQUIRED = re.compile(
    r"cc[\s-]?by|attribution|share[\s-]?alike|gfdl|coverr license|pexels license",
    re.I,
)

_LICENSE_URLS = {
    "CC0": "https://creativecommons.org/publicdomain/zero/1.0/",
    "Public domain": "https://creativecommons.org/publicdomain/mark/1.0/",
    "Creative Commons Attribution": "https://creativecommons.org/licenses/by/4.0/",
    "Creative Commons Attribution-ShareAlike": "https://creativecommons.org/licenses/by-sa/4.0/",
    "GNU Free Documentation License": "https://www.gnu.org/licenses/fdl-1.3.html",
    "Pixabay Content License": "https://pixabay.com/service/license-summary/",
    "Pexels License": "https://www.pexels.com/license/",
    "Coverr License": "https://coverr.co/license",
    "NASA media usage guidelines": "https://www.nasa.gov/nasa-brand-center/images-and-media/",
}


def _clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def resolve_license(hit):
    """Return ``{status, name, url, source_url, evidence, ...}`` for a candidate.

    status:
      * ``confirmed``   – an explicit reusable licence is named by the source
      * ``restricted``  – a licence exists but forbids this use
      * ``unconfirmed`` – the licence is missing, generic or asks to verify
    """
    hit = hit if isinstance(hit, dict) else {}
    raw = _clean(hit.get("license"))
    source = _clean(hit.get("source")) or "Unknown source"
    page_url = _clean(hit.get("page_url"))
    evidence = raw or "(no licence metadata supplied by the provider)"

    status = "unconfirmed"
    name = "Unknown"
    notes = ""

    if raw:
        for pattern_name, pattern in _RESTRICTED_PATTERNS:
            if pattern.search(raw):
                status, name = "restricted", pattern_name
                notes = "The stated licence does not permit this use."
                break
        else:
            for pattern_name, pattern in _REUSABLE_PATTERNS:
                if pattern.search(raw):
                    status, name = "confirmed", pattern_name
                    break
            if status != "confirmed":
                if _UNCONFIRMED_HINTS.search(raw):
                    notes = "The provider text asks for manual verification; it is not a grant."
                else:
                    notes = "The provider text does not name a recognised reusable licence."

    # A confirmed licence must also be traceable to the asset page; without it
    # the claim cannot be audited later.
    if status == "confirmed" and not page_url and not _clean(hit.get("url")):
        status = "unconfirmed"
        notes = "No source page or file URL is available to audit the licence."

    if not notes:
        notes = "Explicit reusable licence named by the source."

    return {
        "status": status,
        "name": name,
        "url": _LICENSE_URLS.get(name, page_url if status == "confirmed" else ""),
        "source_url": page_url or _clean(hit.get("url")),
        "provider": source,
        "provider_host": (urlparse(page_url).hostname or "").lower(),
        "evidence": evidence[:400],
        "attribution_required": bool(status == "confirmed" and _ATTRIBUTION_REQUIRED.search(raw)),
        "reuse_allowed": status == "confirmed",
        "checked": raw != "",
        "notes": notes,
    }


def missing_license_report(scenes):
    """Summarise unconfirmed/restricted footage still referenced by the plan."""
    problems = []
    for index, scene in enumerate(scenes or [], 1):
        info = scene.get("license_info") or {}
        if not info:
            continue
        if info.get("status") != "confirmed":
            problems.append({
                "scene": index,
                "source": scene.get("source") or scene.get("selected_footage_source"),
                "page_url": scene.get("page_url") or scene.get("selected_footage_url"),
                "status": info.get("status"),
                "evidence": info.get("evidence"),
            })
    return problems
