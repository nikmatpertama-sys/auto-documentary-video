"""MM6 research + fact-checking stage (zero cost, no API key required).

The documentary generator must not invent facts. This module provides a
deterministic, free research layer built on the public Wikipedia/Wikimedia
MediaWiki API (no account, no key, no billing) and a claim-verification step
whose reasoning is delegated to whichever free LLM route the pipeline already
uses.

Design rules:

* Research output is stored in ``research.json`` so every script can be traced
  back to the material it was written from.
* Wikipedia text is CC BY-SA; the source URL and licence are recorded and the
  final video credits include them.
* Verification never claims success just because an HTTP call returned 200: the
  model must return a per-claim verdict and evidence, and contradictions fail
  the run.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote

import requests

USER_AGENT = (
    "AutoDocumentaryVideo/6.0 (zero-cost documentary generator; "
    "+https://github.com/nikmatpertama-sys/auto-documentary-video)"
)
WIKIPEDIA_API = "https://{lang}.wikipedia.org/w/api.php"
RESEARCH_TEXT_BUDGET = 6000  # characters injected into the scriptwriter prompt
MAX_ARTICLES_PER_LANGUAGE = 2


def _get(url, params, timeout=(10, 30)):
    response = requests.get(
        url,
        params=params,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def _search_titles(lang, topic, limit=4):
    data = _get(
        WIKIPEDIA_API.format(lang=lang),
        {
            "action": "query",
            "list": "search",
            "srsearch": topic,
            "srlimit": limit,
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


def research_topic(topic, languages=("id", "en"), pause_seconds=0.4):
    """Collect encyclopaedic background for ``topic`` from free sources.

    Returns a dict with ``articles``, ``brief`` and ``warnings``. A topic with no
    matching article is reported honestly instead of being papered over.
    """
    topic = str(topic or "").strip()
    if not topic:
        raise RuntimeError("Research stage received an empty topic.")

    articles = []
    warnings = []
    for lang in languages:
        try:
            titles = _search_titles(lang, topic)
        except Exception as exc:  # network/API failure must be reported, not hidden
            warnings.append(f"{lang}.wikipedia search failed: {type(exc).__name__}: {exc}")
            continue
        for hit in titles[:MAX_ARTICLES_PER_LANGUAGE]:
            try:
                article = _fetch_article(lang, hit["title"])
            except Exception as exc:
                warnings.append(
                    f"{lang}.wikipedia article '{hit['title']}' failed: "
                    f"{type(exc).__name__}: {exc}"
                )
                continue
            if article:
                articles.append(article)
            time.sleep(pause_seconds)

    if not articles:
        warnings.append(
            "No encyclopaedic source was retrieved. The scriptwriter must keep the "
            "narration to widely established knowledge and avoid specific dates, "
            "numbers or quotations."
        )

    return {
        "topic": topic,
        "articles": articles,
        "brief": build_research_brief(articles),
        "warnings": warnings,
        "verified_source_count": len(articles),
        "retrieved_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def build_research_brief(articles, budget=RESEARCH_TEXT_BUDGET):
    """Compact, prompt-ready evidence block. Never fabricates sources."""
    if not articles:
        return ""
    chunks = []
    remaining = max(1000, int(budget))
    per_article = max(600, remaining // max(1, len(articles)))
    for article in articles:
        text = re.sub(r"\s+", " ", str(article.get("extract") or "")).strip()
        if not text:
            continue
        excerpt = text[:per_article]
        chunks.append(
            f"[{article.get('language')}] {article.get('title')} "
            f"({article.get('url')})\n{excerpt}"
        )
    return "\n\n".join(chunks)[:budget]


_CLAIM_MARKERS = re.compile(
    r"\d|%|tahun|abad|million|juta|ribu|kilometer|km|meter|kali|lebih|paling|"
    r"pertama|terbesar|tertinggi|tercepat|satu-satunya|hanya|selalu|tidak pernah|"
    r"year|century|first|largest|only|never",
    re.IGNORECASE,
)


def extract_claims(script):
    """Heuristic list of factual sentences that need evidence."""
    sentences = re.split(r"(?<=[.!?])\s+", str(script or "").strip())
    claims = []
    for sentence in sentences:
        clean = re.sub(r"\s+", " ", sentence).strip()
        if len(clean.split()) < 4:
            continue
        if _CLAIM_MARKERS.search(clean):
            claims.append(clean)
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
        "- supported: the claim is consistent with the source material or is a "
        "widely established fact.\n"
        "- unsupported: the source material does not contain enough evidence for "
        "this specific number/date/superlative.\n"
        "- contradicted: the script conflicts with the source material.\n"
        "- overall is fail if any claim is contradicted, revise if any claim is "
        "unsupported, otherwise pass."
    )


def summarise_verification(payload, claims):
    """Normalise a verifier response and derive a conservative verdict."""
    items = []
    raw_items = payload.get("claims") if isinstance(payload, dict) else None
    if not isinstance(raw_items, list):
        raw_items = []
    for index, claim in enumerate(claims):
        entry = raw_items[index] if index < len(raw_items) and isinstance(raw_items[index], dict) else {}
        verdict = str(entry.get("verdict") or "unsupported").strip().lower()
        if verdict not in ("supported", "unsupported", "contradicted"):
            verdict = "unsupported"
        items.append({
            "claim": claim,
            "verdict": verdict,
            "evidence": str(entry.get("evidence") or "")[:600],
        })

    contradicted = [item for item in items if item["verdict"] == "contradicted"]
    unsupported = [item for item in items if item["verdict"] == "unsupported"]
    supported = [item for item in items if item["verdict"] == "supported"]
    if contradicted:
        overall = "fail"
    elif unsupported:
        overall = "revise"
    else:
        overall = "pass"
    return {
        "claims": items,
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
