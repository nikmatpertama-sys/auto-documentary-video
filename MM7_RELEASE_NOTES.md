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

---

# MM7.1 — production reliability (real-provider paths)

This pass makes the already-shipped MM7 pipeline trustworthy when it runs with
real providers, instead of only under mocks. No new feature surface was added.

## 1. Fact-checking is evidence-bound and covers the whole script

- `research.claims_prompt` now asks the model to classify **every narration
  sentence** as either a factual claim or non-factual, so an empty claim list is
  a valid answer ("the script makes no factual claim") instead of being
  indistinguishable from a failed extraction. `parse_claim_analysis` +
  `claim_coverage` normalise and measure that.
- `main.analyse_script_claims` re-reads any sentence the model skipped and adds
  it as a claim. Skipped sentences are also recorded in `fact_check.json`
  (`skipped_sentences`), so no claim disappears without a note.
- `research.fact_check_prompt` now numbers the retrieved sources (`S1`, `S2`, …)
  and requires, per claim: verdict, evidence, a verbatim quote and the source id.
  `summarise_verification` stores the cited source title/URL, marks
  `evidence_type` (`source_quote`, `general_knowledge`, `unsupported`) and
  downgrades any "supported" verdict without evidence (or with fewer than four
  words of evidence) to `unsupported`.
- Verdicts are no longer warning-only: contradicted claims abort the run;
  unsupported claims are repaired (two attempts) and re-verified; if any remain,
  the run stops unless `ALLOW_UNSUPPORTED_CLAIMS=1` explicitly keeps them, in
  which case they are flagged in `fact_check.json`.
- Failures are persisted (`status`: `no_factual_claims`, `extraction_failed`,
  `insufficient_claims`, `contradicted`, `unsupported`) before the exception, so
  the artifact always explains what happened.

## 2. AI Vision is a real gate with an audit trail

- The downloaded file is still the only thing judged: three frames are extracted
  from the actual clip and sent to the configured vision provider.
- `extract_vision_frames(..., save_dir=...)` keeps the exact frames the model
  saw in `output/vision/scene_XX/` for the accepted clip.
- Each scene records `visual_verification.status` (`approved` / `unverified`),
  provider, confidence, reason, frame paths and the number of rejected
  candidates. Every rejection (visual, download, missing licence) is stored with
  its source, id, title, query, provider, confidence and reason.
- `footage_verification.json` summarises the run: accepted asset + licence per
  scene, all rejections, queries tried, unverified scene list.
- With no vision credentials and `FOOTAGE_VISION_REQUIRED=true` (default) the
  stage fails before any candidate is considered. In the explicit metadata-only
  mode a scene is marked `unverified` and printed as such — never as approved.

## 3. Licence confirmation is separate from visual relevance

- New `footage_license.py` resolves a candidate's licence to `confirmed`,
  `restricted` or `unconfirmed`, with the provider text, source page, licence
  URL and attribution requirement kept as evidence.
- Only explicit reusable licences (CC0, public domain, CC BY, CC BY-SA, GFDL,
  Pixabay/Pexels/Coverr licences, NASA media guidelines) are confirmed.
  "See file page", "verify page terms", empty values and search-snippet wording
  are `unconfirmed`; NC/ND/all-rights-reserved are `restricted`.
- With `FOOTAGE_LICENSE_REQUIRED=true` (default) an unconfirmed licence is
  rejected **before download**, and the rejection is logged. Licence problems in
  the final plan are surfaced by QC (`footage_licences_confirmed`).

## 4. Discovery budget scales with the script

- `configure_footage_budget(scene_count)` derives each provider's per-run ceiling
  from the real scene count (Commons `40 + 16 × scenes`, capped at 900, etc.),
  and prints the resulting budget.
- When a source's remaining allowance is nearly spent, the current scene is
  limited to three query variants instead of exhausting the budget, so later
  scenes still get discovery. Cached queries do not consume it.
- Deduplication uses both `source:id` and a normalised title/filename signature,
  so the same clip cannot be reused under another id or scene.
- No scene or footage quota is enforced: the writer chooses 4–12 scenes from the
  story, and a scene that finds no relevant, licensed, visually verified clip
  fails loudly rather than accepting filler.

## 5. Speechify is the only production voice-over path

- A Speechify error now aborts the run instead of silently switching to Edge TTS.
  Edge TTS is used only when Speechify is not configured at all, or when
  `ALLOW_VOICE_FALLBACK=1` is explicitly set for a local test, and the provider
  is then recorded as `Edge TTS fallback`.
- `output/voiceover_manifest.json` stores provider, model, voice id, locale,
  language, script hash, audio files and per-sentence durations.
  `timing.json` and `stage_report.json` carry the same provider/voice/model.
- The generated audio is still validated (mean/peak dBFS) before rendering.

## 6. Pipeline order

`main()` now runs exactly:

RESEARCH → SCRIPTWRITING → FACT-CHECKING → VOICE-OVER (Speechify) → FOOTAGE
DISCOVERY → DOWNLOAD → AI VISION → RENDERING → FINAL QC.

The duration-recovery loop (extend script → re-fact-check → regenerate VO) runs
inside the voice-over stage, before any footage is searched, so footage always
follows the narration that will be spoken. A regression test asserts the stage
order from the AST of `main()`.

## Verification performed

- `python -m unittest discover`: **161 tests, OK** (23 new tests in
  `test_mm7_production_reliability.py`: licence resolution and gating,
  evidence-bound verification, claim coverage, strict Speechify, unverified
  vision reporting, pipeline order, story-driven scene count).
- Local end-to-end render (`tools/local_e2e.py`): 1080×1920, 83.61 s video,
  83.59 s VO, QC `pass` including the new footage checks.
- Live Wikimedia Commons discovery with real metadata for the three acceptance
  topics; every returned licence resolved correctly (`CC0`, `CC BY 3.0`,
  `CC BY-SA 4.0`, `Public domain` → confirmed). The Amazon LiDAR clip is
  discovered with a valid public-domain licence, which is exactly why licence
  confirmation cannot replace visual verification.

## Not verified in this environment (blocked, not simulated)

No provider credentials exist in this workspace, so these paths are implemented
and unit-tested but have **not** been exercised against a live provider:

- live semantic claim extraction + verification (Groq / OpenRouter / Gemini /
  Cloudflare),
- live AI Vision acceptance/rejection of downloaded clips,
- live Speechify voice-over generation,
- the full production run for "Ant colony life cycle",
  "Amazon rainforest canopy aerial" and "Volcano eruption".

Required secrets for that run: at least one of `GROQ_API_KEY`,
`OPENROUTER_API_KEY`, `GEMINI_API_KEY`/`GOOGLE_API_KEY`, or
`CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID`, plus `SPVO_API` /
`SPEECHIFY_API_KEY` for Speechify narration. Mock results were not treated as
proof of these integrations.
