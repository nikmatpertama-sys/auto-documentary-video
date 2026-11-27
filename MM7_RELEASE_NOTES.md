# MM7 — production failure fixes

MM7 fixes the three failures seen in the last real production run
(topic: "siklus kehidupan semut", 111 tests green):

1. research used irrelevant articles ("Ekosistem", "Rayap"),
2. fact-checking reported PASS with 0 claims checked,
3. footage discovery returned zero candidates for scene 1
   ("No candidate passed the metadata relevance filter").

The fixes target the causes, not the messages or the tests.

## 1. Research

`research.py` now scores every Wikipedia search hit before using it.

- Only the article **title** can establish relevance; a snippet that merely
  mentions a topic word (Rayap = "semut putih", Ekosistem = "siklus") scores
  zero and is discarded. Rejected hits are recorded in
  `research.json → search_diagnostics`.
- Treatment words ("life cycle", "siklus kehidupan") and camera words
  ("aerial", "macro", "drone") are removed from the topic terms, so the subject
  (`semut`) drives the search and the fallback query.
- The topic's own language is probed first. The accepted article's Wikipedia
  language links then supply the counterpart article in the other language, so
  Indonesian and English evidence are both used without machine translation.
- Titles dominated by a specific event or year rank below the article that is
  about the topic itself ("Amazon rainforest" before "2019 Amazon rainforest
  wildfires").
- The brief is built from the intro plus the paragraphs that actually discuss
  the topic, not from whatever the article happens to start with.

Verified live: `siklus kehidupan semut` → id *Semut*, id *Koloni semut*,
en *Ant*, en *Ant colony*; `Amazon rainforest canopy aerial` → *Rainforest*,
*Amazon rainforest wildfires* (kept, ranked below), plus id *Hutan hujan*.

## 2. Fact-checking

- Claims are identified **semantically**: a model lists every statement that
  asserts a verifiable fact (`claims_prompt` / `parse_claims`). A declarative
  sentence heuristic is the fallback, so claims are no longer limited to
  sentences containing digits.
- `summarise_verification` requires non-empty evidence per claim. A "supported"
  verdict without evidence is downgraded to unsupported, and a claim the
  verifier skipped is recorded as unsupported with an explicit reason.
- `fact_check_plan` aborts when fewer than three claims could be checked:
  a PASS with zero checked claims is no longer possible.
- The verification result (overall, checked, supported, unsupported,
  contradicted, claim source) is written into `plan.json → fact_check` and
  `fact_check.json`, and the repair prompt consumes it.

## 3. Footage discovery

- **Query planning** (`footage_queries.py`): short, subject-preserving query
  layers, synonym variants and a subject-only query. Provider APIs AND their
  terms, which is why "ant colony underground tunnels macro" returned literally
  nothing from every source.
- **Provider relaxation**: Wikimedia Commons and Internet Archive retry shorter
  variants of the same query when the full one finds nothing.
- **Commons metadata**: `videoinfo` supplies real durations and English
  categories, and the filename is part of the searchable text, so non-English
  file names are no longer invisible to relevance scoring.
- **Partial metadata**: a candidate whose metadata names the scene subject but
  lacks detail is queued for the visual model (bounded, and never ahead of an
  admitted candidate) instead of being discarded unread.
- **Web search**: a separate `Web Search` source follows indexed result pages to
  their embedded `og:video`/`<video>`/`.mp4` sources. Only pages that state a
  reuse licence, or belong to a public-media host, are accepted, so discovery
  cannot become "download whatever a search result shows".
- **Anchors**: only the subject the query *leads* with is mandatory. A later
  word ("ants carrying **leaves**") qualifies the shot and no longer vetoes
  correct footage.
- **Ranking**: metadata that names the exact subject beats metadata that names
  only its broad category, so an ant colony clip outranks a generic "insect on a
  leaf" clip. The visual model remains the acceptance gate.
- **Budgets**: the Commons per-run ceiling rose from 48 to 200 requests (paced at
  2 s) and the per-scene query cap fell from 8 to 6, because 48 requests were
  exhausted before the last scenes of a real run.

## 4. Language foundation

The narration/VO default is English again (`SCRIPT_LANGUAGE=en`,
`en-US-GuyNeural`); `SCRIPT_LANGUAGE=id` keeps the Indonesian path.
Research still uses Indonesian and English sources for the English script.

## 5. Verification performed

- `python -m unittest discover` → **138 tests pass** (112 previous + 26 new MM7
  tests; two MM6 language tests were updated to the English default).
- Live research for three topics (Indonesian and English).
- Live footage discovery + real download + `ffprobe` for three topics with the
  real pipeline code (`fetch_footage`), vision stubbed:
  - ant colony → `File:A Spring Morning with a Carpenter Ant Colony…webm` (CC0)
  - Amazon canopy → `File:Amazon Canopy Comes to Life through Laser Data.webm`
  - volcano → `File:MT Slamet Volcano eruption on August 26, 2014.webm`
- Local end-to-end render harness (`tools/local_e2e.py`): 83.59 s VO, 1080×1920
  output, QC `pass`.

### Not verified in this environment

- Scriptwriting, semantic claim extraction, fact-check verification and AI vision
  calls need Groq/OpenRouter/Gemini/Cloudflare keys. **No provider key is
  available in this workspace**, so those stages were exercised with mocked
  providers only. A full research → script → fact-check → vision run still has to
  be executed where the keys are configured.
- Google and DuckDuckGo HTML search answer scripted requests with 429/202
  anti-bot pages from this host, so the page-level web discovery path is covered
  by unit tests rather than a live fetch.
