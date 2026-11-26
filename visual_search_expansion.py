"""Conservative secondary search terms for scenes with sparse footage results.

These variants broaden wording, not visual acceptance rules: every downloaded clip
still has to pass the normal pixel-level Vision check unless the dedicated,
strict Vision-outage policy applies.
"""
from __future__ import annotations

import re


_STOP = {
    "the", "and", "for", "with", "from", "into", "that", "this", "over",
    "global", "worldwide", "published", "institutional", "official", "trusted",
    "of", "under", "through", "about", "near", "video", "cinematic",
    "footage", "close", "up", "wide", "angle", "slow", "motion",
    "beautiful", "nature", "scene", "view", "real", "world", "camera",
    "showing", "moving", "during", "while", "where", "what", "when",
    "which", "their", "there", "they", "these", "those", "onto", "its",
    "are", "was", "were", "been", "being", "has", "have", "had", "will",
    "would", "could", "should", "can", "may", "one", "two", "first",
    "second", "then", "than", "also", "more", "most", "very", "some",
    "many", "such", "like", "make", "made", "shot", "shots", "rising",
    "rise", "rises", "mist", "fog", "haze", "cloud", "clouds", "morning",
    "evening", "sunrise", "sunset", "dawn", "dusk", "golden",
}

_GROUPS = {
    "rainforest": {"amazon", "rainforest", "jungle", "tropical", "tropics", "equatorial"},
    "forest": {"forest", "woodland", "woods", "timberland"},
    "canopy": {"canopy", "canopies", "treetop", "treetops", "foliage", "crown", "leaf", "leaves"},
    "river": {"river", "rivers", "waterway", "tributary", "stream"},
    "waterfall": {"waterfall", "falls", "cascade", "cascades"},
    "rain": {"rain", "rainfall", "precipitation", "downpour", "raindrop", "raindrops", "raining", "monsoon"},
    "insect": {"insect", "insects", "bug", "bugs", "ant", "ants", "beetle", "beetles", "butterfly", "butterflies", "mosquito", "mosquitoes", "dragonfly", "dragonflies"},
    "orchid": {"orchid", "orchids", "orchidaceae"},
    "jaguar": {"jaguar", "jaguars"},
    "bird": {"bird", "birds", "avian", "parrot", "parrots", "macaw", "macaws"},
    "wildlife": {"wildlife", "animal", "animals", "fauna"},
    "ocean": {"ocean", "sea", "marine", "underwater"},
    "desert": {"desert", "arid", "dunes", "sand"},
    "mountain": {"mountain", "mountains", "mountainous", "peak", "peaks", "alpine"},
}

_SPECIFIC = ("jaguar", "orchid", "insect", "bird")

# Leading words that are settings or qualifiers rather than a filmable named
# subject; searching them alone returns generic scenery.
_GENERIC_SUBJECTS = {
    "global", "tropical", "rainfall", "precipitation", "rain", "water",
    "river", "stream", "forest", "rainforest", "region", "regional", "world",
    "area", "landscape", "nature", "wildlife", "country", "riverbed",
}


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", str(text or "").lower())


def _present_groups(text: str) -> set[str]:
    words = set(_tokens(text))
    return {name for name, aliases in _GROUPS.items() if words & aliases}


_REFERENCE_ARTIFACT_WORDS = {
    "map", "maps", "chart", "charts", "graph", "graphs", "diagram", "diagrams",
    "table", "tables", "infographic", "infographics", "illustration", "illustrations",
    "animation", "animations", "visualization", "visualizations", "visualisation",
    "visualisations", "graphic", "graphics", "schematic", "schematics", "reference",
}


def _clean_query(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9 ]", " ", str(text or ""))[:120].strip()


def prepare_footage_queries(
    visual_query: str,
    alternative_queries: tuple[str, ...] | list[str] = (),
) -> list[str]:
    """Turn over-specified map/graphic prompts into searchable real-footage subjects."""
    raw = [visual_query, *list(alternative_queries or [])]
    result: list[str] = []
    seen: set[str] = set()
    for value in raw:
        words = [w for w in _tokens(value) if w not in _REFERENCE_ARTIFACT_WORDS]
        query = _clean_query(" ".join(words))
        key = query.lower()
        if query and key not in seen:
            result.append(query)
            seen.add(key)
    return result[:3]


def _fallbacks_for_one_query(query: str) -> list[str]:
    words = [w for w in _tokens(query) if w not in _STOP]
    if not words:
        return []

    generated: list[str] = []
    core = " ".join(words[:7]).strip()
    if len(core.split()) >= 2:
        generated.append(core)

    groups = _present_groups(query)
    has_rainforest = "rainforest" in groups
    has_forest = "forest" in groups or has_rainforest
    environment = "tropical rainforest" if has_rainforest else ("forest" if has_forest else "")

    # Specific living subjects remain mandatory in every generated alternative.
    specific = [name for name in _SPECIFIC if name in groups]
    if "jaguar" in specific:
        generated.append("jaguar in tropical rainforest")
    elif "orchid" in specific:
        generated.append("orchid in tropical rainforest")
    elif "insect" in specific:
        generated.append("tropical rainforest insects")
    elif "bird" in specific:
        generated.append("tropical rainforest birds")
    elif "river" in groups and environment:
        generated.append(f"river through {environment}")
    elif "waterfall" in groups and environment:
        generated.append(f"waterfall in {environment}")
    elif "rain" in groups and has_rainforest:
        generated.extend([
            "rain falling in tropical rainforest",
            "raindrops on tropical rainforest leaves",
        ])
    elif has_rainforest and "canopy" in groups:
        generated.extend([
            "tropical rainforest canopy",
            "Amazon rainforest treetops",
            "jungle canopy",
            "rainforest foliage",
        ])
    elif has_rainforest:
        generated.append("Amazon rainforest landscape")
    elif has_forest and "canopy" in groups:
        generated.append("forest canopy")
    elif "ocean" in groups:
        generated.append("ocean wildlife")
    elif "desert" in groups:
        generated.append("desert landscape")
    elif "mountain" in groups:
        generated.append("mountain landscape")

    return generated


def expand_visual_search_variants(raw_queries: tuple[str, ...] | list[str]) -> list[str]:
    """Build high-recall stock-footage searches without starving alternate shots.

    Each source query is expanded independently. The final bounded list is
    interleaved so the primary shot cannot consume every search slot before
    either of the two independent alternatives is tried.
    """
    replacements = (
        (r"\bAmazon\b", "tropical rainforest"),
        (r"\bcanopy\b", "treetops"),
        (r"\bsunrise\b", "dawn"),
        (r"\baerial view\b", "drone view"),
    )
    per_query: list[list[str]] = []

    for raw in raw_queries or []:
        base = _clean_query(" ".join(
            word for word in _tokens(raw)
            if word not in _REFERENCE_ARTIFACT_WORDS
        ))
        if not base:
            continue

        variants = [base]
        variant = base
        for pattern, replacement in replacements:
            variant = re.sub(pattern, replacement, variant, flags=re.IGNORECASE)
        if variant.lower() != base.lower():
            variants.append(_clean_query(variant))

        core_words = [word for word in _tokens(base) if word not in _STOP]
        core = _clean_query(" ".join(core_words))
        if core and core.lower() != base.lower():
            variants.append(core)
        # A rare subject is often buried in a descriptive phrase the stock index
        # does not match ("beaver resting in stream" returned only streams).
        # Searching the bare subject retrieves its whole library, and the Vision
        # check still rejects any clip that does not show it.
        if len(core_words) > 1 and core_words[0] not in _GENERIC_SUBJECTS:
            variants.append(core_words[0])

        groups = _present_groups(base)
        if "rain" in groups and "rainforest" in groups:
            variants.extend([
                "rain falling in tropical rainforest",
                "raindrops on tropical rainforest leaves",
                "heavy rain over rainforest canopy",
            ])
        elif "rain" in groups and "forest" in groups:
            variants.extend([
                "rain falling through forest canopy",
                "raindrops on forest leaves",
            ])
        elif "rainforest" in groups and any(word in _tokens(base) for word in ("cooling", "climate", "temperature")):
            variants.extend([
                "mist above tropical rainforest canopy",
                "sunlight filtering through tropical rainforest canopy",
            ])

        clean_variants: list[str] = []
        local_seen: set[str] = set()
        for value in variants:
            query = _clean_query(value)
            key = query.lower()
            if query and key not in local_seen:
                clean_variants.append(query)
                local_seen.add(key)
        if clean_variants:
            per_query.append(clean_variants)

    # Round-robin across the primary query and its independent alternatives.
    unique: list[str] = []
    seen: set[str] = set()
    max_depth = max((len(items) for items in per_query), default=0)
    for depth in range(max_depth):
        for variants in per_query:
            if depth >= len(variants):
                continue
            query = variants[depth]
            key = query.lower()
            if key not in seen:
                unique.append(query)
                seen.add(key)
            if len(unique) >= 8:
                return unique
    return unique


def build_visual_search_fallbacks(
    visual_query: str,
    alternative_queries: tuple[str, ...] | list[str] = (),
) -> list[str]:
    """Return broader, subject-preserving queries, independently per shot.

    Weather, mood and descriptive filler are removed without dropping a
    specific organism. Alternatives remain independent so one scene can widen
    searches for multiple valid shots instead of blending their subjects.
    """
    inputs = prepare_footage_queries(visual_query, alternative_queries)

    initial = {x.lower() for x in inputs}
    result: list[str] = []
    seen = set(initial)
    for source_query in inputs:
        added_for_this_shot = 0
        for value in _fallbacks_for_one_query(source_query):
            query = _clean_query(value)
            key = query.lower()
            if query and key not in seen:
                result.append(query)
                seen.add(key)
                added_for_this_shot += 1
            if added_for_this_shot >= 2:
                break
            if len(result) >= 8:
                return result
    return result


def effective_candidate_limit(requested: object) -> int:
    """Apply the production verification minimum despite legacy workflow envs."""
    try:
        value = int(requested)
    except (TypeError, ValueError):
        value = 8
    # A scarce subject (a named animal, a specific structure) often has no
    # acceptable clip in the first eight ranked candidates. The verifier still
    # checks every downloaded pixel, so a wider sweep costs time, not accuracy.
    return max(8, min(20, value))
