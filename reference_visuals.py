"""Policy/helpers for sourced science visuals; never draw replacement diagrams."""
from __future__ import annotations

import re
from urllib.parse import urlparse

REFERENCE_TERMS = (
    r"\bmap\b", r"\bsatellite\b", r"\bchart\b", r"\bgraph\b",
    r"\bdiagram\b", r"\btable\b", r"\binfographic\b",
    r"\bcross[- ]section\b", r"\banatomy\b", r"\bschematic\b",
    r"\bdata visualization\b", r"\bwater cycle\b", r"\bfood web\b",
    r"\btimeline\b", r"\bdistribution map\b", r"\bscientific illustration\b",
    r"\billustration\b", r"\banimation\b", r"\banimated illustration\b",
    r"\bscientific animation\b", r"\bexplanatory animation\b", r"\bmotion graphic\b",
    r"\bexplainer graphic\b", r"\bvisualization\b", r"\bscientific visual\b",
    r"\bscientific model\b", r"\bclimate model\b", r"\bmedical illustration\b",
    r"\bgeological section\b", r"\btopographic\b",
)


def needs_reference_visual(scene: dict, scene_index: int | None = None) -> bool:
    """Return true for explicit evidence visuals, never for a generic narration word.

    Scene one is always kept as footage so the opening title/hook has a stable
    video background and is never lost behind a still-reference frame.
    """
    if scene_index == 1:
        return False
    mode = str(scene.get("visual_mode") or scene.get("visual_type") or "").lower()
    if mode in {"reference", "trusted_reference", "reference_visual", "map", "chart", "diagram", "table"}:
        return True
    search_text = " ".join([
        str(scene.get("visual_query", "")),
        " ".join(str(x) for x in scene.get("visual_queries", []) if x),
    ]).lower()
    return any(re.search(pattern, search_text, flags=re.I) for pattern in REFERENCE_TERMS)


TRUSTED_DOMAINS = (
    "nasa.gov", "noaa.gov", "usgs.gov", "esa.int", "who.int", "nih.gov",
    "cdc.gov", "un.org", "unep.org", "worldbank.org", "gov", "edu",
    "nature.com", "science.org", "sciencedirect.com", "nationalgeographic.org",
    "nps.gov", "fws.gov", "epa.gov", "usda.gov", "si.edu", "smithsonian.org",
)


def allowed_reuse_license(license_name: str) -> bool:
    """Accept open licenses with reuse/modification; reject NC and ND terms."""
    value = re.sub(r"\s+", "", str(license_name or "").lower())
    if not value:
        return False
    if any(token in value for token in (
        "noncommercial", "non-commercial", "by-nc", "ccby-nc",
        "bynd", "by-nd", "ccby-nd", "no-derivatives", "noderivatives",
    )):
        return False
    return any(token in value for token in (
        "cc0", "publicdomain", "public-domain", "public domain",
        "ccby", "cc-by", "ccby-sa", "cc-by-sa", "creativecommonsattribution",
    ))


def has_trusted_institution_url(url: str = "") -> bool:
    """Institutional provenance must be demonstrated by the actual source URL."""
    host = (urlparse(str(url or "")).hostname or "").lower()
    return bool(host) and any(
        host == domain or host.endswith("." + domain)
        for domain in TRUSTED_DOMAINS
    )


def query_match_ratio(candidate_text: str, query: str) -> float:
    stop = {"the", "and", "for", "with", "from", "over", "under", "image", "photo", "official", "map", "diagram"}
    q = {x for x in re.findall(r"[a-z0-9]+", str(query).lower()) if len(x) > 2 and x not in stop}
    c = {x for x in re.findall(r"[a-z0-9]+", str(candidate_text).lower()) if len(x) > 2}
    if not q:
        return 0.0
    return len(q & c) / len(q)


def validate_reference_candidate(candidate: dict, query: str, min_match: float = 0.50) -> tuple[bool, str]:
    if not candidate.get("url") or not candidate.get("page_url"):
        return False, "Missing media or source-page URL."
    if not allowed_reuse_license(candidate.get("license", "")) and candidate.get("source") != "NASA":
        return False, "No clearly reusable license metadata."
    source = str(candidate.get("source") or "").lower()
    original_url = str(candidate.get("original_source_url") or "").strip()
    page_url = str(candidate.get("page_url") or "").strip()
    # A Commons uploader's free-text creator/credit field is not proof of
    # provenance. Require a linked primary institutional source page.
    if source == "wikimedia commons":
        if not original_url or not has_trusted_institution_url(original_url):
            return False, "Commons file has no linked primary source on a trusted institutional domain."
    elif not has_trusted_institution_url(original_url or page_url):
        return False, "No authoritative institutional source URL was identified."
    metadata = " ".join([
        str(candidate.get("title", "")), str(candidate.get("metadata", "")),
        str(candidate.get("publisher", "")), str(candidate.get("creator", "")),
    ])
    ratio = query_match_ratio(metadata, query)
    if ratio < min_match:
        return False, f"Source metadata match {ratio:.2f} is below {min_match:.2f}."
    return True, f"Institutional source and metadata match {ratio:.2f}."
