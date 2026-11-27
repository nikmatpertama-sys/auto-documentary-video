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
CLAIM_EVIDENCE_BUDGET = 5000  # extra characters retrieved for unsupported claims
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
# CLAIM-TARGETED EVIDENCE
# ------------------------------------------------------------

# Words that carry no retrieval signal. Keeping them would make every claim
# search the same generic articles.
CLAIM_STOPWORDS = frozenset(
    """
    a an the and or but if then than that this these those it its it's they them their
    there here was were is are be been being has have had do does did not no nor so
    such as at by for from in into of on onto over under to up with within without
    which who whom whose when where why how all any both each few more most other
    some very can could may might must shall should will would also just only even
    still yet about after before during while through across between among around
    one two three first second new many much own same s t itself themselves we our
    you your he she his her i me my mine us them because until whether per via
    """.split()
)


def claim_search_terms(claim, limit=7):
    """Distinctive terms from a claim, in the order they appear."""
    seen = []
    for token in _tokens(claim):
        if len(token) < 4 or token in CLAIM_STOPWORDS or token.isdigit():
            continue
        if token not in seen:
            seen.append(token)
        if len(seen) >= limit:
            break
    return seen


def _passages_for_claim(article, terms, budget, max_passages=2):
    """Passages of this article that actually discuss the claim's terms."""
    text = re.sub(r"\s+", " ", str(article.get("extract") or "")).strip()
    if not text:
        return []
    paragraphs = [part.strip() for part in re.split(r"(?<=[.!?])\s{2,}", text) if part.strip()]
    if len(paragraphs) <= 1:
        paragraphs = [part.strip() for part in text.split("\n") if part.strip()]
    term_set = set(terms)
    scored = []
    for index, paragraph in enumerate(paragraphs):
        overlap = len(term_set & set(_tokens(paragraph)))
        if overlap >= 2:
            scored.append((overlap, index, paragraph))
    scored.sort(key=lambda item: (-item[0], item[1]))
    chosen = []
    used = 0
    for _, _, paragraph in scored[:max_passages]:
        if used >= budget:
            break
        chosen.append(paragraph[:budget])
        used += len(paragraph)
    return chosen


def gather_claim_evidence(
    claims,
    languages=("en",),
    per_claim_budget=900,
    total_budget=CLAIM_EVIDENCE_BUDGET,
    max_claims=6,
    pause_seconds=0.3,
):
    """Retrieve encyclopaedic passages that specifically address each claim.

    The first research pass summarises the topic, which is not the same as
    supporting the mechanism a sentence claims. When a claim is unsupported by
    that summary, the fairest next step is to look for the passage that actually
    discusses it, instead of only rewording the script. Nothing here fabricates
    evidence: every passage comes from a fetched article and keeps its URL.
    """
    wanted = []
    for claim in claims or []:
        claim = re.sub(r"\s+", " ", str(claim or "")).strip()
        if claim and claim not in wanted:
            wanted.append(claim)
    wanted = wanted[:max_claims]

    languages = [str(lang).strip() for lang in (languages or ("en",)) if str(lang).strip()]
    if not languages:
        languages = ["en"]

    blocks = []
    articles = []
    queries = []
    warnings = []
    used = 0
    seen_titles = set()

    for claim in wanted:
        terms = claim_search_terms(claim)
        if not terms:
            continue
        query = " ".join(terms)
        queries.append(query)
        found = None
        for language in languages:
            try:
                hits = _search_titles(language, query, limit=5)
            except Exception as exc:  # pragma: no cover - network dependent
                warnings.append(f"{language} search failed: {type(exc).__name__}: {exc}")
                continue
            for score, _, hit in rank_search_hits(hits, terms):
                if score <= 0:
                    continue
                key = (language, hit["title"].lower())
                if key in seen_titles:
                    continue
                try:
                    article = _fetch_article(language, hit["title"])
                except Exception as exc:  # pragma: no cover - network dependent
                    warnings.append(
                        f"{language}:{hit['title']} fetch failed: {type(exc).__name__}: {exc}"
                    )
                    continue
                if pause_seconds:
                    _sleep(pause_seconds)
                if not article:
                    continue
                passages = _passages_for_claim(article, terms, per_claim_budget)
                if not passages:
                    continue
                seen_titles.add(key)
                articles.append(article)
                found = {
                    "claim": claim,
                    "title": article["title"],
                    "url": article["url"],
                    "language": article["language"],
                    "passages": passages,
                }
                break
            if found:
                break

        if not found:
            warnings.append(f"no passage found for claim: {claim[:120]}")
            continue

        block = (
            f"[{found['language']}] {found['title']} ({found['url']})\n"
            "CLAIM ADDRESSED: " + found["claim"] + "\n"
            + "\n".join(found["passages"])
        )
        if used + len(block) > total_budget:
            break
        blocks.append(block)
        used += len(block)

    return {
        "brief": "\n\n".join(blocks),
        "articles": articles,
        "queries": queries,
        "warnings": warnings,
        "characters": used,
    }


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
    """Prompt asking the model to classify every narration sentence.

    The model must account for the *whole* script: each sentence is either a
    verifiable factual claim or explicitly non-factual. That makes an empty
    claim list a legitimate answer (a purely atmospheric script) instead of an
    indistinguishable extraction failure, and it exposes sentences the model
    silently skipped.
    """
    return (
        "You are a documentary fact-checking editor. Read the complete narration "
        "and classify every sentence into exactly one of two lists.\n\n"
        f"NARRATION:\n{script}\n\n"
        "Return ONLY this JSON object:\n"
        '{"claims": [{"claim": "sentence copied verbatim", '
        '"kind": "behaviour|quantity|date|place|cause|identification|comparison|other"}], '
        '"non_factual": ["sentence copied verbatim"]}\n'
        "Rules:\n"
        "- Copy each sentence exactly as written; never paraphrase, split or merge."
        "\n- claims: sentences that assert a verifiable fact about the world "
        "(a mechanism, a behaviour, a quantity, a date, a place, a cause, an "
        "identification or a comparison).\n"
        "- non_factual: sentences that are only mood, imagery, a rhetorical "
        "question, a transition or a value judgement.\n"
        "- Every sentence of the narration must appear exactly once across the "
        "two lists. Do not skip sentences.\n"
        "- Never invent a sentence that is not in the narration.\n"
        "- A narration with no factual statement returns an empty claims list; "
        "that is a valid answer, not an error."
    )


def parse_claim_analysis(payload):
    """Normalise claim extraction into claims plus explicitly non-factual text."""
    claims = parse_claims(payload)
    raw_non_factual = payload.get("non_factual") if isinstance(payload, dict) else None
    non_factual = []
    seen = {claim.lower() for claim in claims}
    if isinstance(raw_non_factual, list):
        for item in raw_non_factual:
            text = item.get("sentence") if isinstance(item, dict) else item
            text = re.sub(r"\s+", " ", str(text or "")).strip()
            if len(text.split()) < 2 or text.lower() in seen:
                continue
            seen.add(text.lower())
            non_factual.append(text)
    return {"claims": claims, "non_factual": non_factual}


def claim_coverage(script, claims, non_factual):
    """Measure how much of the script the extraction actually accounted for."""
    sentences = split_sentences(script)
    normalised = {
        re.sub(r"\W+", " ", sentence).strip().lower()
        for sentence in list(claims) + list(non_factual)
    }
    uncovered = [
        sentence for sentence in sentences
        if re.sub(r"\W+", " ", sentence).strip().lower() not in normalised
    ]
    return {
        "script_sentences": len(sentences),
        "claimed_sentences": len(claims),
        "non_factual_sentences": len(non_factual),
        "uncovered_sentences": uncovered,
        "coverage": (
            round((len(sentences) - len(uncovered)) / len(sentences), 3)
            if sentences else 0.0
        ),
    }


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


def _source_catalogue(sources):
    """Numbered source list with stable ids (S1, S2, ...) for the verifier."""
    catalogue = []
    for index, source in enumerate(sources or [], 1):
        if not isinstance(source, dict):
            continue
        title = re.sub(r"\s+", " ", str(source.get("title") or "")).strip()
        url = str(source.get("url") or "").strip()
        if not title and not url:
            continue
        catalogue.append({"id": f"S{index}", "title": title, "url": url})
    return catalogue


def fact_check_prompt(script, claims, brief, sources=None):
    """Build the verification prompt. Supports are linked to a source id."""
    claim_list = "\n".join(f"{i}. {claim}" for i, claim in enumerate(claims, 1))
    evidence = brief or "(No external source material was retrieved.)"
    catalogue = _source_catalogue(sources)
    source_lines = "\n".join(
        f"{item['id']}: {item['title']} ({item['url']})" for item in catalogue
    ) or "(No source article was retrieved; only universally established knowledge is available.)"
    return (
        "You are a strict documentary fact-checker. You may only use the SOURCE "
        "MATERIAL and SOURCE LIST below plus universally established knowledge. "
        "Do not invent sources, numbers, dates or quotations.\n\n"
        f"SOURCE LIST (cite these ids):\n{source_lines}\n\n"
        f"SOURCE MATERIAL:\n{evidence}\n\n"
        f"SCRIPT:\n{script}\n\n"
        f"CLAIMS TO VERIFY:\n{claim_list}\n\n"
        "Return ONLY this JSON object:\n"
        '{"claims": [{"claim": "the claim", "verdict": "supported|unsupported|'
        'contradicted", "evidence": "short reason", "quote": "verbatim words '
        'from the source material", "source_id": "S1"}], '
        '"overall": "pass|revise|fail"}\n'
        "Rules:\n"
        "- Return exactly one entry per claim listed above, in the same order.\n"
        "- supported: the SOURCE MATERIAL states or directly implies the claim, "
        "or it is a universally established fact. \n"
        "- For a supported claim, copy the supporting words from the SOURCE "
        "MATERIAL into 'quote' (verbatim, at least 4 words) and set source_id "
        "to the source that contains them. If the support is universal "
        "knowledge rather than the supplied material, set source_id to "
        "\"general_knowledge\" and still provide the evidence.\n"
        "- unsupported: the material does not contain enough evidence for this "
        "specific mechanism, number, date or superlative. Do not mark a claim "
        "supported because it sounds plausible.\n"
        "- contradicted: the claim conflicts with the SOURCE MATERIAL; quote the "
        "conflicting words.\n"
        "- every entry needs non-empty evidence; a verdict without evidence is "
        "treated as unsupported.\n"
        "- overall is fail if any claim is contradicted, revise if any claim is "
        "unsupported, otherwise pass."
    )


def summarise_verification(payload, claims, sources=None):
    """Normalise a verifier response and derive a conservative verdict.

    Every claim keeps its verdict, its evidence and — when the verifier cited a
    supplied article — that article's title and URL. A "supported" verdict
    without evidence is downgraded, so a confident-sounding answer alone can
    never pass a claim.
    """
    catalogue = {item["id"]: item for item in _source_catalogue(sources)}
    for index, item in enumerate(_source_catalogue(sources), 1):
        catalogue[str(index)] = item
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
        evidence = re.sub(r"\s+", " ", str(entry.get("evidence") or "")).strip()
        quote = re.sub(r"\s+", " ", str(entry.get("quote") or "")).strip()
        source_key = re.sub(r"[^A-Za-z0-9]", "", str(entry.get("source_id") or "")).upper()
        cited = catalogue.get(source_key)
        support_source = None
        if cited and source_key.lower() not in ("general_knowledge", "general", "none"):
            support_source = {"id": cited["id"], "title": cited["title"], "url": cited["url"]}
        if verdict not in ("supported", "unsupported", "contradicted"):
            verdict = "unsupported"
            if not evidence:
                evidence = "The verifier returned no verdict for this claim."
        elif verdict == "supported" and not evidence:
            verdict = "unsupported"
            evidence = "The verifier gave no supporting evidence for this claim."
        elif verdict == "supported" and len(evidence.split()) < 4:
            verdict = "unsupported"
            evidence = f"Evidence was too thin to verify the claim: {evidence!r}"
        items.append({
            "claim": claim,
            "kind": str(entry.get("kind") or "").strip(),
            "verdict": verdict,
            "evidence": evidence[:600],
            "quote": quote[:400],
            "source": support_source,
            "evidence_type": (
                "source_quote" if verdict == "supported" and support_source and quote
                else "general_knowledge" if verdict == "supported" and support_source is None
                else "unsupported" if verdict == "unsupported"
                else verdict
            ),
        })

    contradicted = [item for item in items if item["verdict"] == "contradicted"]
    unsupported = [item for item in items if item["verdict"] == "unsupported"]
    supported = [item for item in items if item["verdict"] == "supported"]
    unsourced_supported = [
        item for item in supported if item.get("source") is None
    ]
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
        "sourced_supports": len(supported) - len(unsourced_supported),
        "general_knowledge_supports": len(unsourced_supported),
        "sources": _source_catalogue(sources),
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
