"""MM6 research + fact-checking stage (zero cost, no API key required).

The documentary generator must not invent facts. This module provides a
deterministic, free research layer built on the public Wikipedia/Wikimedia
MediaWiki API (no account, no key, no billing) and a claim-verification step
whose reasoning is delegated to whichever free LLM route the pipeline already
uses.

Design rules:

* Relevance is decided before an article is accepted. A Wikipedia search hit
  that merely shares a word with the topic (for example "Ekosistem" or "Rayap"
  for the topic "siklus kehidupan semut") is *not* a source for that topic.
* The topic's own language is searched first, then the matching article in the
  other language is fetched through Wikipedia language links. That bridges
  Indonesian and English sources without machine translation or an API key.
* Research output is stored in ``research.json`` so every script can be traced
  back to the material it was written from.
* Wikipedia text is CC BY-SA; the source URL and licence are recorded and the
  final video credits include them.
* Verification never claims success just because an HTTP call returned 200: the
  model must return a per-claim verdict and evidence, and a run with no checked
  claim is a failure, never a pass.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from urllib.parse import quote

import requests

USER_AGENT = (
    "AutoDocumentaryVideo/7.0 (zero-cost documentary generator; "
    "+https://github.com/nikmatpertama-sys/auto-documentary-video)"
)
WIKIPEDIA_API = "https://{lang}.wikipedia.org/w/api.php"
RESEARCH_TEXT_BUDGET = 6000  # characters injected into the scriptwriter prompt
MAX_ARTICLES_PER_LANGUAGE = 2
SEARCH_HITS_PER_QUERY = 12
# A search hit is only treated as a source when its *title* shares a topic term.
# Snippet-only matches are the classic false positive (e.g. "Rayap" for ants).
TITLE_RELEVANCE_FLOOR = 0.45
SUPPORTED_LANGUAGES = ("en", "id")

# Words that describe the *treatment* of a topic rather than its subject. They
# must not decide whether an article is relevant, and they are dropped when a
# fallback subject query is built ("siklus kehidupan semut" -> "semut").
_TREATMENT_TERMS = {
    "life", "cycle", "cycles", "history", "story", "stories", "science",
    "scientific", "documentary", "explained", "explainer", "guide", "overview",
    "introduction", "facts", "fact", "amazing", "secret", "secrets", "world",
    "process", "stages", "stage", "phases", "phase", "how", "why", "what",
    "siklus", "kehidupan", "hidup", "sejarah", "kisah", "ilmu", "sains",
    "dokumenter", "proses", "tahapan", "fakta", "faktafakta", "rahasia",
    "dunia", "mengapa", "bagaimana", "apa",
}

# Camera/shot words describe how a documentary is filmed, not what it is about.
# They must not decide which encyclopaedia article matches the topic.
_SHOT_TERMS = {
    "aerial", "drone", "overhead", "closeup", "close", "macro", "wide",
    "timelapse", "footage", "video", "broll", "cinematic", "4k", "8k",
    "shot", "scene", "clip", "stock", "slowmotion",
}

_STOPWORDS = {
    "the", "and", "of", "to", "in", "on", "for", "with", "from", "by", "at",
    "a", "an", "is", "are", "was", "were", "be", "been", "that", "this",
    "these", "those", "it", "its", "as", "or", "into", "about", "over",
    "under", "within", "their", "there", "yang", "dan", "di", "ke", "dari",
    "dengan", "pada", "adalah", "untuk", "dalam", "itu", "ini", "sebagai",
    "atau", "juga", "akan", "tidak", "bisa", "dapat", "oleh", "para",
}

_INDONESIAN_MARKERS = {
    "yang", "dan", "di", "ke", "dari", "dengan", "pada", "adalah", "untuk",
    "dalam", "itu", "ini", "tidak", "siklus", "kehidupan", "semut", "hutan",
    "laut", "gunung", "sejarah", "proses", "tahapan", "kisah", "dunia",
}
_ENGLISH_MARKERS = {
    "the", "and", "of", "in", "on", "for", "with", "life", "cycle", "history",
    "how", "why", "what", "world", "forest", "ocean", "mountain", "river",
}


def _get(url, params, timeout=(10, 30)):
    response = requests.get(
        url,
        params=params,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def _stem(token):
    """Cheap singular form so "ants"/"ant" and "forests"/"forest" agree."""
    token = str(token or "")
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _tokens(text):
    return [
        _stem(token)
        for token in re.findall(r"[a-z0-9]+", str(text or "").lower())
        if token not in _STOPWORDS and len(token) > 2
    ]


def topic_terms(topic):
    """Content words of the topic, with treatment and camera words removed."""
    raw = [
        token for token in re.findall(r"[a-z0-9]+", str(topic or "").lower())
        if token not in _STOPWORDS and len(token) > 2
    ]
    # Filter the *unstemmed* words first: stemming would turn "siklus" into
    # "siklu" and "forests" into "forest", hiding them from these lists.
    tokens = [
        _stem(token) for token in raw
        if token not in _SHOT_TERMS and token not in _TREATMENT_TERMS
    ]
    return tokens or [_stem(token) for token in raw]


def detect_topic_language(topic):
    """Best-effort language of the topic string (``en``, ``id`` or ``""``)."""
    words = set(re.findall(r"[a-z]+", str(topic or "").lower()))
    indonesian = len(words & _INDONESIAN_MARKERS)
    english = len(words & _ENGLISH_MARKERS)
    if indonesian > english:
        return "id"
    if english > indonesian:
        return "en"
    return ""


def _search_titles(lang, topic, limit=SEARCH_HITS_PER_QUERY):
    data = _get(
        WIKIPEDIA_API.format(lang=lang),
        {
            "action": "query",
            "list": "search",
            "srsearch": topic,
            "srlimit": limit,
            "srsort": "relevance",
            "format": "json",
            "formatversion": 2,
        },
    )
    results = (data.get("query") or {}).get("search") or []
    return [
        {
            "title": item.get("title") or "",
            "snippet": re.sub(r"<[^>]+>", "", str(item.get("snippet") or "")),
        }
        for item in results
        if item.get("title")
    ]


def score_search_hit(hit, terms):
    """Relevance of a Wikipedia search hit to the topic.

    The article *title* is authoritative: a hit whose title does not share a
    topic term is not a source for the topic, however often the word appears in
    its snippet. Titles that are mostly about something else (a specific event,
    a named year, a single species) lose to the article that is about the topic
    itself.
    """
    if not terms:
        return 0.0
    title_tokens = set(_tokens(hit.get("title")))
    snippet_tokens = set(_tokens(hit.get("snippet")))
    term_set = set(terms)
    title_hits = len(term_set & title_tokens)
    snippet_hits = len(term_set & snippet_tokens)
    if not title_hits:
        return 0.0
    title_coverage = title_hits / len(term_set)
    snippet_coverage = snippet_hits / len(term_set)
    # How much of the title is the topic itself, rather than extra qualifiers
    # such as a year or a one-off event.
    focus = title_hits / max(1, len(title_tokens))
    extra_tokens = {
        token for token in title_tokens - term_set
        if not token.isdigit()
    }
    extra_penalty = 0.18 * len(extra_tokens)
    focus_bonus = 0.2 if len(title_tokens) <= 3 else 0.0
    return round(
        max(0.0, 1.5 * title_coverage + 0.5 * snippet_coverage + 0.5 * focus
            + focus_bonus - extra_penalty),
        4,
    )


def rank_search_hits(hits, terms):
    scored = []
    for hit in hits:
        score = score_search_hit(hit, terms)
        scored.append((score, len(_tokens(hit.get("title"))), hit))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return scored


def _langlinks(lang, title):
    """Map of language code -> article title for the same subject."""
    data = _get(
        WIKIPEDIA_API.format(lang=lang),
        {
            "action": "query",
            "prop": "langlinks",
            "lllimit": 500,
            "redirects": 1,
            "titles": title,
            "format": "json",
            "formatversion": 2,
        },
    )
    pages = (data.get("query") or {}).get("pages") or []
    page = pages[0] if pages else {}
    links = page.get("langlinks") or []
    return {
        str(link.get("lang") or ""): str(link.get("title") or "")
        for link in links
        if isinstance(link, dict) and link.get("lang") and link.get("title")
    }


def _fetch_article(lang, title):
    data = _get(
        WIKIPEDIA_API.format(lang=lang),
        {
            "action": "query",
            "prop": "extracts|info",
            "explaintext": 1,
            "exsectionformat": "plain",
            "redirects": 1,
            "inprop": "url",
            "titles": title,
            "format": "json",
            "formatversion": 2,
        },
    )
    pages = (data.get("query") or {}).get("pages") or []
    page = pages[0] if pages else {}
    extract = str(page.get("extract") or "").strip()
    if not extract:
        return None
    return {
        "language": lang,
        "title": page.get("title") or title,
        "url": page.get("fullurl") or f"https://{lang}.wikipedia.org/wiki/{quote(title)}",
        "extract": extract,
        "license": "CC BY-SA 4.0 (Wikipedia contributors)",
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def research_topic(topic, languages=SUPPORTED_LANGUAGES, pause_seconds=0.4):
    """Collect genuinely relevant encyclopaedic background for ``topic``.

    The topic's own language is probed first; accepted articles are then
    completed with their counterpart in the other supported language through
    Wikipedia language links. Returns ``articles``, a prompt-ready ``brief``,
    per-hit ``diagnostics`` and honest ``warnings``.
    """
    topic = str(topic or "").strip()
    if not topic:
        raise RuntimeError("Research stage received an empty topic.")

    terms = topic_terms(topic)
    topic_language = detect_topic_language(topic)
    search_order = []
    if topic_language:
        search_order.append(topic_language)
    search_order.extend(lang for lang in languages if lang not in search_order)

    articles = []
    warnings = []
    diagnostics = []
    accepted_titles = []

    def search_language(lang, query, reason):
        try:
            hits = _search_titles(lang, query)
        except Exception as exc:  # network/API failure must be reported, not hidden
            warnings.append(f"{lang}.wikipedia search failed: {type(exc).__name__}: {exc}")
            return []
        ranked = rank_search_hits(hits, terms)
        diagnostics.append({
            "language": lang,
            "query": query,
            "reason": reason,
            "hits": [
                {"title": hit["title"], "score": score}
                for score, _length, hit in ranked[:8]
            ],
        })
        return [
            (score, hit["title"])
            for score, _length, hit in ranked
            if score >= TITLE_RELEVANCE_FLOOR
        ]

    def store(article, relevance, via):
        for existing in articles:
            if (
                existing["language"] == article["language"]
                and existing["title"].lower() == article["title"].lower()
            ):
                return False
        article = dict(article)
        article["relevance"] = round(float(relevance), 3)
        article["via"] = via
        articles.append(article)
        return True

    # 1. Primary search in the topic's own language (then the remaining ones if
    #    that produces nothing relevant).
    primary_language = None
    for lang in search_order:
        accepted = search_language(lang, topic, "topic query")
        if not accepted:
            continue
        primary_language = lang
        for score, title in accepted[:MAX_ARTICLES_PER_LANGUAGE]:
            try:
                article = _fetch_article(lang, title)
            except Exception as exc:
                warnings.append(
                    f"{lang}.wikipedia article '{title}' failed: {type(exc).__name__}: {exc}"
                )
                continue
            if article and store(article, score, "search"):
                accepted_titles.append((lang, article["title"]))
            if pause_seconds:
                _sleep(pause_seconds)
        break

    # 2. A subject-only query rescues topics whose wording is too specific for
    #    the search engine ("siklus kehidupan semut" -> "semut").
    if not articles:
        subject_query = " ".join(terms[:2]).strip()
        if subject_query and subject_query.lower() != topic.lower():
            for lang in search_order:
                accepted = search_language(lang, subject_query, "subject fallback query")
                if not accepted:
                    continue
                primary_language = primary_language or lang
                for score, title in accepted[:MAX_ARTICLES_PER_LANGUAGE]:
                    try:
                        article = _fetch_article(lang, title)
                    except Exception as exc:
                        warnings.append(
                            f"{lang}.wikipedia article '{title}' failed: "
                            f"{type(exc).__name__}: {exc}"
                        )
                        continue
                    if article and store(article, score, "subject fallback"):
                        accepted_titles.append((lang, article["title"]))
                    if pause_seconds:
                        _sleep(pause_seconds)
                if articles:
                    break

    # 3. Complete the evidence with the same subject in the other language.
    for lang, title in list(accepted_titles):
        if not articles:
            break
        try:
            links = _langlinks(lang, title)
        except Exception as exc:
            warnings.append(f"langlinks for {lang}:{title} failed: {type(exc).__name__}: {exc}")
            continue
        for other_language in search_order:
            if other_language == lang:
                continue
            other_title = links.get(other_language)
            if not other_title:
                continue
            if any(
                article["language"] == other_language
                and article["title"].lower() == other_title.lower()
                for article in articles
            ):
                continue
            try:
                article = _fetch_article(other_language, other_title)
            except Exception as exc:
                warnings.append(
                    f"{other_language}.wikipedia article '{other_title}' failed: "
                    f"{type(exc).__name__}: {exc}"
                )
                continue
            if article:
                store(article, 1.0, f"langlink:{lang}:{title}")
            if pause_seconds:
                _sleep(pause_seconds)

    if not articles:
        warnings.append(
            "No encyclopaedic source was retrieved that actually matches the topic. "
            "The scriptwriter must keep the narration to widely established "
            "knowledge and avoid specific dates, numbers or quotations."
        )
    elif len(articles) == 1:
        warnings.append(
            "Only one source article matched the topic; treat scene-level detail "
            "conservatively."
        )

    return {
        "topic": topic,
        "topic_language": topic_language or primary_language or "",
        "topic_terms": terms,
        "articles": articles,
        "brief": build_research_brief(articles, terms),
        "warnings": warnings,
        "search_diagnostics": diagnostics,
        "verified_source_count": len(articles),
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _sleep(seconds):
    import time

    time.sleep(seconds)


def _focused_extract(article, terms, budget):
    """Intro plus the paragraphs that actually discuss the topic."""
    text = re.sub(r"\s+", " ", str(article.get("extract") or "")).strip()
    if not text:
        return ""
    term_set = set(terms)
    paragraphs = [part.strip() for part in re.split(r"(?<=[.!?])\s{2,}", text) if part.strip()]
    if not paragraphs:
        paragraphs = [text]
    ranked = []
    for index, paragraph in enumerate(paragraphs):
        overlap = len(term_set & set(_tokens(paragraph)))
        ranked.append((index, overlap))
    keep = {0}
    for index, overlap in sorted(ranked, key=lambda item: (-item[1], item[0])):
        if overlap < 1:
            break
        keep.add(index)
        if len(keep) >= 4:
            break
    selected = " ".join(paragraphs[index] for index in sorted(keep))
    return selected[:budget]


def build_research_brief(articles, terms=None, budget=RESEARCH_TEXT_BUDGET):
    """Compact, prompt-ready evidence block. Never fabricates sources."""
    if not articles:
        return ""
    terms = list(terms or [])
    chunks = []
    remaining = max(1000, int(budget))
    per_article = max(700, remaining // max(1, len(articles)))
    for article in articles:
        excerpt = _focused_extract(article, terms, per_article)
        if not excerpt:
            continue
        chunks.append(
            f"[{article.get('language')}] {article.get('title')} "
            f"({article.get('url')})\n{excerpt}"
        )
    return "\n\n".join(chunks)[:budget]


# ------------------------------------------------------------
# FACT CHECKING
# ------------------------------------------------------------

# A factual claim is a declarative sentence, not just a sentence that happens to
# contain a digit. These predicate markers keep the heuristic honest when the
# semantic extraction call is unavailable, while the primary path asks the model
# to list the claims itself.
_FACTUAL_PREDICATES = re.compile(
    r"\b(?:is|are|was|were|has|have|had|can|could|will|may|might|must|live[ds]?|"
    r"grow(?:s|n)?|feeds?|eats?|produc(?:e|es|ed)|contain(?:s|ed)?|"
    r"make[sd]?|uses?|turns?|becom(?:e|es)|covers?|reach(?:es|ed)?|"
    r"depend(?:s|ed)?|need[sd]?|build(?:s|ing)?|carry|carries|carried|"
    r"lay[s]?|laid|hatch(?:es|ed)?|kill(?:s|ed)?|protect(?:s|ed)?|"
    r"allow(?:s|ed)?|travel(?:s|led)?|measur(?:e|es|ed)|found|discover(?:s|ed)?|"
    r"show(?:s|n|ed)?|means?|suggest(?:s|ed)?|hold[s]?|held|keeps?|kept|"
    r"start(?:s|ed)?|ends?|creat(?:e|es|ed)|forms?|includes?|works?|"
    r"helps?|occur(?:s|red)?|remain(?:s|ed)?|surviv(?:e|es|ed)|hunt(?:s|ed)?|"
    r"store[sd]?|releas(?:e|es|ed)|absorb(?:s|ed)?|emit(?:s|ted)?|"
    r"weigh(?:s|ed)?|spans?|compris(?:e|es)|represent(?:s|ed)?|"
    r"adalah|merupakan|memiliki|mempunyai|hidup|tumbuh|makan|memakan|"
    r"menghasilkan|mengandung|membentuk|menjadi|mencakup|menutupi|mencapai|"
    r"bergantung|membutuhkan|membangun|membawa|bertelur|menetas|membunuh|"
    r"melindungi|mengukur|ditemukan|menunjukkan|berarti|membantu|terjadi|"
    r"bertahan|berburu|menyimpan|melepaskan|menyerap|mengeluarkan|terdiri)\b",
    re.IGNORECASE,
)
_EVIDENTIAL_MARKERS = re.compile(
    r"\d|%|percent|million|billion|juta|miliar|ribu|year|century|decade|"
    r"tahun|abad|dekade|first|largest|smallest|only|never|always|most|least|"
    r"fastest|tallest|deepest|oldest|pertama|terbesar|terkecil|tertinggi|"
    r"tercepat|terdalam|tertua|satu-satunya|selalu|tidak pernah|paling",
    re.IGNORECASE,
)


def split_sentences(text):
    return [
        re.sub(r"\s+", " ", sentence).strip()
        for sentence in re.split(r"(?<=[.!?])\s+", str(text or "").strip())
        if sentence.strip()
    ]


def extract_claims(script):
    """Heuristic fallback: every declarative sentence is a factual claim.

    Documentary narration states facts continuously, so digits alone are a poor
    filter. A sentence qualifies when it has enough words to be a statement and
    either names an evidential detail (number/date/superlative) or uses a
    declarative predicate.
    """
    claims = []
    for sentence in split_sentences(script):
        words = sentence.split()
        if len(words) < 5 or sentence.rstrip().endswith("?"):
            continue
        if _EVIDENTIAL_MARKERS.search(sentence) or _FACTUAL_PREDICATES.search(sentence):
            claims.append(sentence)
    return claims


def claims_prompt(script):
    """Prompt asking the model for a semantic list of factual claims."""
    return (
        "You are a documentary fact-checking editor. Read the narration and list "
        "every statement that asserts a verifiable fact about the world: a "
        "mechanism, a behaviour, a quantity, a date, a place, a cause or a "
        "scientific description. Ignore pure scene-setting mood, rhetorical "
        "questions and value judgements.\n\n"
        f"NARRATION:\n{script}\n\n"
        "Return ONLY this JSON object:\n"
        '{"claims": ["claim 1", "claim 2"]}\n'
        "Rules:\n"
        "- Quote each claim as a single self-contained sentence taken from the "
        "narration, verbatim.\n"
        "- List between 3 and 20 claims; a narration with fewer than 3 factual "
        "statements is itself a problem to report as an empty list.\n"
        "- Never invent claims that are not in the narration."
    )


def parse_claims(payload):
    """Normalise a semantic claim-extraction response into a list of sentences."""
    raw = payload.get("claims") if isinstance(payload, dict) else payload
    if isinstance(raw, dict):
        raw = raw.get("claims")
    if not isinstance(raw, list):
        return []
    claims = []
    seen = set()
    for item in raw:
        text = item.get("claim") if isinstance(item, dict) else item
        text = re.sub(r"\s+", " ", str(text or "")).strip()
        if len(text.split()) < 3:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        claims.append(text)
        if len(claims) >= 25:
            break
    return claims


def fact_check_prompt(script, claims, brief):
    """Build the verification prompt. The model must cite the supplied brief."""
    claim_list = "\n".join(f"{i}. {claim}" for i, claim in enumerate(claims, 1))
    evidence = brief or "(No external source material was retrieved.)"
    return (
        "You are a strict documentary fact-checker. You may only use the SOURCE "
        "MATERIAL below plus universally established knowledge. Do not invent "
        "sources, numbers, dates or quotations.\n\n"
        f"SOURCE MATERIAL:\n{evidence}\n\n"
        f"SCRIPT:\n{script}\n\n"
        f"CLAIMS TO VERIFY:\n{claim_list}\n\n"
        "Return ONLY this JSON object:\n"
        '{"claims": [{"claim": "the claim", "verdict": "supported|unsupported|'
        'contradicted", "evidence": "short reason or quoted evidence"}], '
        '"overall": "pass|revise|fail"}\n'
        "Rules:\n"
        "- Return exactly one entry per claim listed above, in the same order.\n"
        "- supported: the claim is consistent with the source material or is a "
        "widely established fact; name the evidence.\n"
        "- unsupported: the source material does not contain enough evidence for "
        "this specific mechanism, number, date or superlative.\n"
        "- contradicted: the script conflicts with the source material.\n"
        "- every entry needs non-empty evidence; an empty verdict is treated as "
        "unsupported.\n"
        "- overall is fail if any claim is contradicted, revise if any claim is "
        "unsupported, otherwise pass."
    )


def summarise_verification(payload, claims):
    """Normalise a verifier response and derive a conservative verdict."""
    items = []
    raw_items = payload.get("claims") if isinstance(payload, dict) else None
    if isinstance(raw_items, list):
        by_text = {}
        for entry in raw_items:
            if isinstance(entry, dict):
                text = re.sub(r"\s+", " ", str(entry.get("claim") or "")).strip().lower()
                if text:
                    by_text[text] = entry
    else:
        by_text = {}

    for index, claim in enumerate(claims):
        entry = {}
        matched = by_text.get(re.sub(r"\s+", " ", str(claim)).strip().lower())
        if matched is not None:
            entry = matched
        elif isinstance(raw_items, list) and index < len(raw_items) and isinstance(raw_items[index], dict):
            entry = raw_items[index]
        verdict = str(entry.get("verdict") or "").strip().lower()
        evidence = str(entry.get("evidence") or "").strip()
        if verdict not in ("supported", "unsupported", "contradicted"):
            verdict = "unsupported"
            if not evidence:
                evidence = "The verifier returned no verdict for this claim."
        elif verdict == "supported" and not evidence:
            # A pass without evidence is not a pass.
            verdict = "unsupported"
            evidence = "The verifier gave no supporting evidence for this claim."
        items.append({
            "claim": claim,
            "verdict": verdict,
            "evidence": evidence[:600],
        })

    contradicted = [item for item in items if item["verdict"] == "contradicted"]
    unsupported = [item for item in items if item["verdict"] == "unsupported"]
    supported = [item for item in items if item["verdict"] == "supported"]
    if not items:
        overall = "fail"
    elif contradicted:
        overall = "fail"
    elif unsupported:
        overall = "revise"
    else:
        overall = "pass"
    return {
        "claims": items,
        "checked": len(items),
        "supported": len(supported),
        "unsupported": len(unsupported),
        "contradicted": len(contradicted),
        "overall": overall,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def write_research_files(research, verification=None):
    from pathlib import Path

    Path("research.json").write_text(
        json.dumps(research, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    if verification is not None:
        Path("fact_check.json").write_text(
            json.dumps(verification, indent=2, ensure_ascii=False), encoding="utf-8"
        )
