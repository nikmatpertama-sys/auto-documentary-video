"""Conservative secondary search terms for scenes with sparse footage results.

These variants broaden wording, not visual acceptance rules: every downloaded clip
still has to pass the normal pixel-level Vision check unless the dedicated,
strict Vision-outage policy applies.
"""
from __future__ import annotations

import re


_STOP = {
    "the", "and", "for", "with", "from", "into", "that", "this", "over",
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


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", str(text or "").lower())


def _present_groups(text: str) -> set[str]:
    words = set(_tokens(text))
    return {name for name, aliases in _GROUPS.items() if words & aliases}


def _clean_query(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9 ]", " ", str(text or ""))[:95].strip()


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


def build_visual_search_fallbacks(
    visual_query: str,
    alternative_queries: tuple[str, ...] | list[str] = (),
) -> list[str]:
    """Return broader, subject-preserving queries, independently per shot.

    Weather, mood and descriptive filler are removed without dropping a
    specific organism. Alternatives remain independent so one scene can widen
    searches for multiple valid shots instead of blending their subjects.
    """
    inputs = [_clean_query(visual_query)]
    inputs.extend(_clean_query(x) for x in alternative_queries)
    inputs = [x for x in inputs if x]

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
    return max(8, min(12, value))
