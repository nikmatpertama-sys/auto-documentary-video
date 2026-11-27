"""Footage query planning.

Stock-archive and web-search APIs treat a multi-word query as an AND of every
term. A natural-language scene query such as "ant colony underground tunnels
macro" therefore returns nothing at all, which is indistinguishable from "no
relevant footage exists". This module builds the missing bridge:

* short, subject-preserving query layers (subject + shot, subject alone),
* synonym variants so a catalog that calls an ant an "insect" is still found,
* provider-side relaxation plans for engines that AND every term.

Nothing here decides whether a clip is acceptable. Every clip produced through a
relaxed query still has to pass the metadata anchors and the AI visual review.
"""

from __future__ import annotations

import re

# Words that describe how a shot is filmed or edited, not what it contains.
SHOT_WORDS = {
    "macro", "closeup", "close", "extreme", "detail", "slowmotion", "slow",
    "motion", "cinematic", "aerial", "drone", "underwater", "timelapse",
    "time", "lapse", "hyperlapse", "broll", "b", "roll", "stock", "clip",
    "footage", "video", "shot", "scene", "wide", "medium", "pov", "4k", "8k",
    "hd", "uhd", "ultra", "high", "quality", "resolution", "footage4k",
}

STOP_WORDS = {
    "the", "and", "for", "with", "from", "into", "that", "this", "over", "of",
    "under", "through", "about", "near", "on", "in", "at", "to", "by", "as",
    "an", "a", "is", "are", "be", "it", "its", "their", "they", "while",
    "during", "inside", "within", "across", "onto", "up", "down", "out",
    "very", "some", "many", "more", "most", "much", "such", "than", "then",
    "also", "into", "over", "around", "between", "behind", "above", "below",
}

# Canonical subject -> search aliases. Deliberately conservative: synonyms only
# widen the search, they never lower the acceptance bar.
SYNONYM_GROUPS = {
    "ant": {"ant", "ants", "formicidae", "ant colony", "ant nest", "insect"},
    "termite": {"termite", "termites", "isoptera", "white ant"},
    "bee": {"bee", "bees", "honeybee", "honey bee", "apis", "pollinator"},
    "wasp": {"wasp", "wasps", "hornet", "vespa"},
    "butterfly": {"butterfly", "butterflies", "moth", "lepidoptera", "caterpillar"},
    "beetle": {"beetle", "beetles", "coleoptera"},
    "spider": {"spider", "spiders", "arachnid", "web", "tarantula"},
    "insect": {"insect", "insects", "bug", "bugs", "arthropod", "invertebrate"},
    "worm": {"worm", "worms", "earthworm", "annelid", "larva"},
    "plankton": {"plankton", "zooplankton", "phytoplankton", "krill"},
    "bird": {"bird", "birds", "avian", "parrot", "macaw", "eagle", "hummingbird"},
    "fish": {"fish", "fishes", "reef fish", "school of fish", "tropical fish"},
    "shark": {"shark", "sharks", "great white", "hammerhead"},
    "whale": {"whale", "whales", "humpback", "orca", "cetacean"},
    "dolphin": {"dolphin", "dolphins", "porpoise"},
    "frog": {"frog", "frogs", "toad", "amphibian", "tadpole"},
    "snake": {"snake", "snakes", "serpent", "python", "cobra"},
    "lizard": {"lizard", "lizards", "gecko", "iguana", "reptile"},
    "mammal": {"mammal", "mammals", "animal", "animals", "wildlife", "fauna"},
    "tree": {"tree", "trees", "trunk", "timber", "woodland"},
    "forest": {"forest", "forests", "woodland", "woods", "jungle", "rainforest"},
    "rainforest": {"rainforest", "rain forest", "jungle", "tropical forest", "amazon"},
    "canopy": {"canopy", "treetop", "treetops", "tree crown", "foliage"},
    "leaf": {"leaf", "leaves", "foliage", "frond"},
    "flower": {"flower", "flowers", "bloom", "blossom", "petal"},
    "orchid": {"orchid", "orchids", "orchidaceae"},
    "moss": {"moss", "mosses", "lichen", "bryophyte"},
    "fungus": {"fungus", "fungi", "mushroom", "mushrooms", "mycelium"},
    "bacteria": {"bacteria", "bacterium", "microbe", "microbes", "microorganism"},
    "cell": {"cell", "cells", "cellular", "microscopic", "microscope"},
    "coral": {"coral", "corals", "reef", "coral reef"},
    "ocean": {"ocean", "sea", "marine", "seawater", "underwater"},
    "river": {"river", "rivers", "stream", "tributary", "waterway"},
    "waterfall": {"waterfall", "waterfalls", "cascade", "falls"},
    "desert": {"desert", "dunes", "arid", "sand"},
    "glacier": {"glacier", "glaciers", "ice", "iceberg", "ice sheet"},
    "volcano": {"volcano", "volcanoes", "lava", "eruption", "magma"},
    "mountain": {"mountain", "mountains", "peak", "summit", "range"},
    "cave": {"cave", "caves", "cavern", "stalactite"},
    "soil": {"soil", "earth", "dirt", "sediment", "ground"},
    "seed": {"seed", "seeds", "germination", "sprout"},
    "root": {"root", "roots", "root system", "rhizome"},
    "nest": {"nest", "nests", "burrow", "burrows", "mound", "tunnel", "tunnels"},
    "colony": {"colony", "colonies", "superorganism", "hive"},
    "larva": {"larva", "larvae", "grub", "caterpillar", "pupa", "pupae", "cocoon"},
    "egg": {"egg", "eggs", "brood"},
    "queen": {"queen", "queens", "queen ant", "monarch"},
    "predator": {"predator", "predators", "hunter", "hunting", "prey"},
    "climate": {"climate", "weather", "atmosphere", "temperature", "climate model"},
    "satellite": {"satellite", "satellites", "spaceborne", "remote sensing"},
    "space": {"space", "cosmos", "orbit", "galaxy", "nebula", "planet"},
    "mars": {"mars", "martian", "red planet", "rover"},
    "moon": {"moon", "lunar", "luna", "crater"},
    "city": {"city", "cities", "urban", "skyline", "street"},
    "ship": {"ship", "ships", "vessel", "cargo ship", "boat"},
    "factory": {"factory", "factories", "industrial", "machinery", "assembly line"},
    "human": {"human", "people", "person", "man", "woman", "crowd"},
    "brain": {"brain", "neuron", "neurons", "nervous system", "synapse"},
    "blood": {"blood", "vein", "veins", "artery", "circulation"},
    "skeleton": {"skeleton", "bone", "bones", "skull", "fossil"},
}

# Groups that name a specific organism or object rather than a habitat or scene.
# Only these get the "metadata must name the subject" rule; a generic forest clip
# is a legitimate answer to a forest scene, but a beetle is not an ant colony.
NARROW_SUBJECTS = {
    "ant", "termite", "bee", "wasp", "butterfly", "beetle", "spider",
    "worm", "fish", "shark", "whale", "dolphin", "frog", "snake", "lizard",
    "orchid", "moss", "fungus", "bacteria", "cell", "coral", "plankton",
    "larva", "queen",
}

GENERIC_ALIASES = {
    "insect", "insects", "bug", "bugs", "arthropod", "invertebrate",
    "animal", "animals", "wildlife", "fauna", "plant", "plants",
    "tree", "trees", "nature", "colony", "colonies", "nest", "nests",
    "burrow", "tunnel", "egg", "eggs", "predator", "hive",
}


def normalize(text):
    return re.findall(r"[a-z0-9']+", str(text or "").lower())


def specific_terms(canonical):
    """Aliases that genuinely name a narrow subject (empty for habitat groups)."""
    if not canonical or canonical not in NARROW_SUBJECTS:
        return set()
    terms = set()
    for alias in SYNONYM_GROUPS.get(canonical, ()):
        parts = normalize(alias)
        if len(parts) == 1 and parts[0] not in GENERIC_ALIASES:
            terms.add(parts[0])
    return terms


# Single-word aliases map directly; multi-word aliases are matched as phrases so
# a shared word cannot mislabel the subject ("tropical fish" must never make
# "tropical rainforest" resolve to the fish group).
_ALIAS_TO_CANONICAL = {}
_PHRASE_ALIASES = []
for _canonical, _aliases in SYNONYM_GROUPS.items():
    for _alias in _aliases:
        _parts = normalize(_alias)
        if len(_parts) == 1:
            _ALIAS_TO_CANONICAL.setdefault(_parts[0], _canonical)
        elif _parts:
            _PHRASE_ALIASES.append((_canonical, _parts))


def content_terms(text):
    """Meaningful query terms, in their original order, de-duplicated."""
    terms = []
    seen = set()
    for token in normalize(text):
        if token in STOP_WORDS or len(token) < 2:
            continue
        if token in seen:
            continue
        seen.add(token)
        terms.append(token)
    return terms


def canonical_subject(terms):
    """Canonical synonym group of the most subject-like term, if any."""
    term_set = set(terms)
    best = ""
    best_length = 0
    for canonical, parts in _PHRASE_ALIASES:
        if len(parts) > best_length and set(parts) <= term_set:
            best, best_length = canonical, len(parts)
    if best:
        return best
    for term in terms:
        if term in SHOT_WORDS:
            continue
        canonical = _ALIAS_TO_CANONICAL.get(term)
        if canonical:
            return canonical
    return ""


def subject_phrases(text, max_alias=4):
    """The subject phrase of a query plus its strongest aliases, in a stable order.

    The phrase that literally appears in the query comes first ("ant colony"
    before "ant"), then the canonical subject, then the remaining aliases. The
    order is deterministic so the generated searches can be reproduced.
    """
    terms = content_terms(text)
    term_set = set(terms)
    canonical = canonical_subject(terms)
    if not canonical:
        # No known synonym group: the first non-shot term is the subject.
        for term in terms:
            if term not in SHOT_WORDS:
                return [term] if term else []
        return []

    aliases = []
    in_query = []
    for alias in SYNONYM_GROUPS.get(canonical, ()):
        cleaned = " ".join(normalize(alias))
        if not cleaned or cleaned in aliases:
            continue
        aliases.append(cleaned)
        if set(normalize(cleaned)) <= term_set:
            in_query.append(cleaned)
    in_query.sort(key=lambda value: (-len(value.split()), len(value)))

    ordered = []
    for phrase in in_query + [canonical] + aliases:
        if phrase and phrase not in ordered:
            ordered.append(phrase)
    return ordered[:max_alias]


def layered_queries(query, narration="", limit=6):
    """Short-to-long query layers that keep the scene's subject intact.

    The full natural-language query is kept first (it is the most precise), and
    truncated/expanded layers follow because providers AND their terms. Every
    layer still names the scene's subject, so a short query never turns into a
    random search.
    """
    base_terms = content_terms(query)
    if not base_terms:
        base_terms = content_terms(narration)[:2]
    if not base_terms:
        return []

    subject = subject_phrases(query) or subject_phrases(narration) or base_terms[:1]
    subject_words = set(normalize(subject[0]))
    shot_terms = [term for term in base_terms if term in SHOT_WORDS][:1]
    qualifiers = [
        term for term in base_terms
        if term not in SHOT_WORDS and term not in subject_words
    ][:2]

    layers = [" ".join(base_terms)]
    for alias in subject[:2]:
        if qualifiers:
            layers.append(" ".join([alias] + qualifiers))
        if shot_terms:
            layers.append(" ".join([alias] + shot_terms))
    # The bare subject phrase is the highest-yield query of all: catalogs index
    # "ant colony" but rarely "ant colony underground tunnels macro".
    layers.append(subject[0])
    layers.append(" ".join(subject[:1] + qualifiers[:1]))
    layers.append(" ".join(subject[:1] + shot_terms[:1]))
    layers.append(" ".join(qualifiers[:2] + subject[:1]))

    ordered = []
    seen = set()
    for layer in layers:
        cleaned = " ".join(content_terms(layer)) if layer else ""
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        ordered.append(cleaned)
        if len(ordered) >= max(1, int(limit)):
            break
    return ordered


def provider_query_variants(query, max_variants=4):
    """Progressively shorter variants for engines that AND every term."""
    terms = content_terms(query)
    if not terms:
        return []
    variants = [" ".join(terms)]
    if len(terms) > 3:
        variants.append(" ".join(terms[:3]))
    if len(terms) > 2:
        variants.append(" ".join(terms[:2]))
    subject = subject_phrases(query)
    if subject:
        variants.append(subject[0])
    variants.append(terms[0])

    ordered = []
    seen = set()
    for variant in variants:
        if not variant or variant in seen:
            continue
        seen.add(variant)
        ordered.append(variant)
        if len(ordered) >= max(1, int(max_variants)):
            break
    return ordered
