# Auto Documentary Video — MM7

Automated vertical documentary generator for Facebook Reels. It researches a
 topic, writes a grounded English documentary script of whatever length the story
 needs, ends it with a spoken follow + comment call to action, fact-checks
 its claims, searches and visually verifies real-world footage, generates a
 voice-over and reference-style subtitles, mixes a royalty-free music bed and
 sound design, renders a 1080x1920 video that ends with the narration, and gates
 the result through a final quality-control report.

Production line:

```
TOPIC -> RESEARCH -> SCRIPT -> FACT-CHECK -> FOOTAGE SEARCH -> VISUAL MATCHING
      -> VOICE-OVER -> EDITING -> AUDIO MIX -> RENDER -> QUALITY CONTROL
```

MM6 details are documented in [`MM6_RELEASE_NOTES.md`](MM6_RELEASE_NOTES.md);
the MM7 production-failure fixes are in
[`MM7_RELEASE_NOTES.md`](MM7_RELEASE_NOTES.md).

## MM7 — production failure fixes

- **Research relevance:** search hits are scored before use. A hit whose *title*
  does not share a topic word is rejected, so "siklus kehidupan semut" no longer
  researches the topic from "Ekosistem" or "Rayap". The topic's own language is
  searched first and its article's Wikipedia language links supply the matching
  English/Indonesian counterpart (`research.json` also records rejected hits).
- **Fact-checking:** claims are identified semantically (a model lists the
  factual statements) with a declarative-sentence fallback. Every claim must
  carry a verdict *and* evidence, and a run with fewer than three checked claims
  fails instead of reporting PASS.
- **Footage discovery:** queries are planned in short, subject-preserving layers
  with synonym variants; every provider retries a shorter query when the full
  one returns nothing (all these APIs AND their terms). Wikimedia Commons uses
  `videoinfo` for real durations and English categories, and candidates whose
  metadata only partly names the subject are sent to the visual model instead of
  being discarded. A new `Web Search` source follows indexed pages to their
  embedded `og:video`/`<video>`/`.mp4` sources, but only when the page states a
  reusable licence.
- **Ranking:** naming the exact subject outranks naming its broad category, so an
  ant colony clip wins over a generic "insect on a leaf" clip.

## MM6 — reference-style production stages

- **Language:** English (`SCRIPT_LANGUAGE=en`) is the project default, narrated
  with `en-US-GuyNeural`; `SCRIPT_LANGUAGE=id` switches the script, captions and
  voice to Bahasa Indonesia (`id-ID-ArdiNeural`). Captions, hook and vocabulary
  checks follow the same setting.
- **Research:** `research.py` reads the public Wikipedia API (no key) and stores
  the evidence, URLs and licences in `research.json`.
- **Fact-checking:** claims are identified per sentence (semantically in MM7),
  verified against the research brief, repaired once when needed, and a
  surviving contradiction stops the run (`fact_check.json`).
- **Duration:** the measured voice-over decides the video length. If it is under
  70 seconds the script is deepened or the delivery is slowed naturally and the
  VO regenerated; silence padding is never used.
- **Presentation:** bold centred uppercase subtitles with `MarginV=420` (above
  the Reels UI), an ASS-based opening hook, bundled DejaVu fonts (`fonts/`), a
  locally synthesised music bed ducked under the narration, and procedural
  whoosh/impact sound design. No third-party audio is downloaded.
- **Quality control:** `qc_report.json` verifies resolution, duration, VO
  alignment, audio level/clipping, black gaps, captions, word count, scene count
  and footage reuse after the render.
- **Asset documentation:** `copyright_report.json` records the licence and
  provenance of every clip, the voice provider, the generated audio and the font.

## Master scriptwriter prompt

`SCRIPTWRITER_MASTER_PROMPT.md` remains the single source of truth for the generator's professional documentary scriptwriter role. `main.py` loads it at runtime through `documentary_prompt()`. The prompt enforces factual discipline, a story-driven scene count (4–12 scenes, normally 6–10, never padded to a quota), a narration with no word minimum or maximum (the measured 70-second voice-over is the only length rule), a mandatory closing follow + comment CTA, and practical footage queries. Its claim-by-claim audit is part of the same prompt; it does not create a second independent fact-check API call.

## Footage discovery — MM5 (MM4 source retained)

The active footage search retains the MM4 Coverr integration (when `COVERR_API_KEY` is configured), Internet Archive, Wikimedia Commons, NASA, Web Search (page-embedded reusable video) and Google Search for directly indexed reusable video files. MM7 adds layered query planning, provider-side query relaxation and synonym bridging, because these catalogs AND every term in a query and a natural-language scene description used to return no hits at all. Coverr is queried only for the primary scene query and one targeted retry, using `is_vertical:true`, `is_premium:false`, and `is_ai_generated:false`; catalog aspect ratios other than 9:16 are discarded before download, and `ffprobe` must still confirm the actual downloaded file is native 9:16. Coverr remains a native-vertical source; MM5's landscape reframing applies to other eligible high-relevance candidates only after separate AI crop planning and post-crop visual verification. This uses Coverr's official REST API rather than scraping its website. The open archives are preferred when candidates are similarly relevant; Google Search is a discovery index, not a video host. Ordinary YouTube/social pages are not scraped or downloaded. Google HTML results can be rate-limited or changed, so its candidates are ranked behind comparable archive results.

Coverr is optional: the workflow already maps `COVERR_API_KEY` from GitHub Actions secrets. If the secret is blank or unavailable, Coverr is skipped and all previous sources continue normally. Coverr's current license (updated 2026-11-04) allows commercial use of stock library footage, while the free Demo API tier requires attribution to Coverr and the creator; the retained MM4 integration adds that attribution to `credits.txt`. The API's documented Demo allowance is 50 requests/hour, so the integration caps its own Coverr searches at 18 per run and caches results. It excludes premium/watermarked and AI-generated library clips. See the [Coverr API video filters](https://coverr.co/api/docs/videos), [API rate limits](https://coverr.co/api/docs/start), and [current Coverr license](https://coverr.co/license/).

Pexels and Pixabay remain **disabled by default** because their catalogs were repeatedly too limited or mismatched for this project. They can only be enabled intentionally with `ENABLE_PEXELS_FALLBACK=true` or `ENABLE_PIXABAY_FALLBACK=true` in the workflow environment, and their API keys. MM7.4 enables Pixabay in `render.yml` (its key is configured, and the visual gate still reviews every clip); Pexels stays off because no credential is configured. Text providers are Groq, OpenRouter, Gemini, DeepSeek (`DEEPSEEK_MODEL`, OpenAI-compatible) and Cloudflare Workers AI, selected by the quota-aware router.

MM4 adds an API-backed vertical-footage source:
- Uses the existing `COVERR_API_KEY` workflow secret and official Coverr search API, with free/non-premium, authentic (non-AI), vertical filters.
- Requires Coverr's catalog ratio to be 9:16 when present, then keeps the shared FFprobe check as the final truth. The existing mandatory visual verification remains unchanged.
- Limits Coverr to at most two targeted requests per scene and 18 per running job, observes the shared source cooldown/cache system, and writes free-Demo API attribution into `credits.txt`.
- Does not enable Pexels or Pixabay. Coverr candidates still must be native 9:16; MM5's separate landscape crop path does not change Coverr's source filters.


## MM5 — adaptive downloads and AI-verified vertical reframing

MM5 is built directly from MM4, so the Coverr source, its API filters, shared cooldown/cache integration, free-Demo attribution, and existing source priority are all retained.

- **Adaptive download budgets:** ordinary candidates keep the 180 MB limit; candidates scoring at least 0.72 may use up to 650 MB, and candidates scoring at least 0.86 may use up to 900 MB. Known file sizes are checked before transfer where the provider exposes them.
- **Streaming and resume:** footage downloads are streamed in 1 MiB chunks; interrupted transfers make a bounded HTTP Range resume attempt when supported. A 15-minute time budget and 1.5 GB free-disk reserve protect the runner. Servers that do not support Range are safely restarted rather than having duplicate bytes appended.
- **Native portrait remains preferred:** Coverr is still searched with its native vertical/free/non-AI filters and its downloaded files must pass the actual FFprobe check. Any suitable native 9:16 candidate remains preferred over a crop-dependent landscape candidate.
- **Landscape eligibility:** only high-relevance downloaded landscape sources with a display aspect between 1.18 and 2.40 and at least 640 px on the short side may enter the reframing path. Square, non-9:16 portrait, low-resolution and unsupported sources are rejected.
- **AI crop planning:** Gemini, Groq or OpenRouter vision reviews six timestamped frames and returns subject/composition boxes. Every required box must fit within a 9:16 crop with safety margins and confidence of at least 0.55. Excessive pan speed, missing boxes or uncertain composition rejects the candidate.
- **Dynamic subject tracking:** a smoothed FFmpeg crop follows the AI-provided horizontal tracking path. The path loops safely if a short source clip is repeated to fill a longer scene.
- **Post-crop verification:** three frames from the proposed reframed view are sent through the normal visual verifier. A landscape candidate is selected only if the cropped result remains relevant; otherwise it is rejected and discovery continues.
- **Fail-closed behavior:** if multi-frame crop vision is unavailable, landscape candidates are not reframed. Native 9:16 candidates can still proceed through the regular visual-verification path.
- **Preserved constraints:** Pixabay/Pexels remain disabled, Coverr stays a native-vertical source, scene relevance and license checks remain in force, and no landscape is accepted merely by scaling or padding.

The MM4 workflow template already maps `COVERR_API_KEY`, includes the FFmpeg runtime and runs `python -m unittest discover -v`. The ZIP extractor intentionally preserves the permanent `.github` workflow already installed in the repository; this MM5 upgrade introduces no new secret or required YML change. The combined local suite contains 87 unit/integration tests. Local tests use mocked provider replies and synthetic media; they do not constitute a live Coverr, Gemini, Groq, OpenRouter or archive download test.

MM1 adds four footage-intelligence improvements:
- Intent-aware ranking separates title, tags, descriptive metadata, scene subject, requested action, and camera viewpoint. A title/tag match weighs more than an incidental word buried in a long description.
- Strong off-topic contexts such as military training, urban traffic, industrial scenes, sports, and agriculture are filtered when they are not part of the scene intent.
- Generic forest/tree metadata is never treated as proof of a rainforest match; it may enter the relaxed AI-gated queue only when the request still has enough supporting context, and it cannot satisfy a missing mandatory shot-type anchor by itself.
- If the first candidate batch is exhausted, one bounded second search wave creates up to four subject-preserving queries informed by the visual-review rejection reason and checks at most twelve additional candidates.

With the default `FOOTAGE_VISION_REQUIRED=true`, every selected clip must produce a verified 9:16 result and pass minimum resolution/duration, metadata relevance, and visual-AI checks. Native 9:16 remains the first choice. MM2 fixes one false-negative in discovery: catalog width/height values can describe encoded dimensions without exposing the file's 90-degree display rotation. The search stage uses catalog dimensions only for a low-resolution precheck and a positive portrait-ranking hint; it defers the final aspect-ratio decision until the actual downloaded file is inspected by `ffprobe_video_info()`, which applies display rotation and sample-aspect-ratio metadata. MM5 can additionally consider high-relevance landscape sources only when the downloaded source is within its supported resolution/aspect bounds, an AI crop planner confirms the full scene-critical content fits a moving 9:16 window, and a separate post-crop visual verification passes. Square, 4:5, unsupported landscape, low-resolution, low-confidence, or crop-unsafe sources remain rejected. The pipeline remains fail-closed: it does not lower the visual confidence threshold or silently substitute unrelated footage. Metadata-only selection is possible only when the operator explicitly disables required visual verification; landscape reframing is unavailable in that mode. Wikimedia 429s trigger a persistent timed cooldown instead of short repeat requests; source 5xx/timeouts also cool down temporarily, while downloads use bounded transient retries. License information must be checked against the original source page before publication; Google-indexed license snippets are not a substitute for the source's actual terms.

`footage.py` is retained as a harmless legacy compatibility shim; it no longer contains the old hard-coded Pixabay downloader and is not part of the active `render.py` → `main.py` pipeline.

## Automatic Markdown cleanup — carried forward from 10.21

`main.py` removes generated upgrade notes and validation-report Markdown files from the local project workspace at startup and after success or failure. Version 10.21 adds `persist_generated_markdown.py`, which stages and commits only those matching tracked files that are already gone from the workspace. The supplied `render.yml` calls it after artifact upload even when video generation fails. MM3 also uses that same step to persist the small, non-secret source cooldown state. The archive also carries an optional `.github/workflows/cleanup-generated-markdown.yml` template for users who want cleanup on ordinary pushes; it is not active in a branch unless copied there manually. `README.md`, `SCRIPTWRITER_MASTER_PROMPT.md`, unrelated documentation, and unrelated staged changes are protected.

**Workflow note:** the ZIP-upgrade workflow intentionally preserves the repository's `.github` directory instead of replacing workflows from a project ZIP. On the current `main` branch, the permanent `render.yml` already calls `persist_generated_markdown.py` after artifact upload (with `if: always()`), so generated Markdown deletions are persisted after both successful and failed video runs. The separate cleanup workflow template is optional and only needed for cleanup on ordinary pushes; it is not required for the video-run cleanup path. A local run of `main.py` deletes files from the extracted working directory; it does not rewrite the original ZIP archive or push changes to GitHub by itself.

## GitHub Actions workflow preservation

The archive contains a snapshot of `.github/workflows/render.yml` for reference and local setup. The automated ZIP installer deliberately skips the `.github` directory so it cannot overwrite the repository's permanent workflow. The cleanup template is optional; the active render workflow already persists cleanup after video runs. Keep the repository's existing render workflow unchanged; MM3 uses the already-present always-run persistence step to save the small source cooldown state file.

## Required GitHub Secrets

Add one or more AI provider credentials. The pipeline automatically discovers numbered keys such as `GROQ_API_KEY`, `GROQ_API_KEY_1`, `GROQ_API_KEY_2`; the same convention works for `OPENROUTER_API_KEY` and `GEMINI_API_KEY`.

Cloudflare Workers AI supports paired credentials: `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID`, and the additional aliases `CF_API_TK1` + `CF_ID1` (or numbered pairs 2, 3, etc.). It is included as an additional text and visual-verification fallback. `PIXABAY_API_KEY` and `PEXELS_API_KEY` remain optional footage-source keys. Internet Archive, Wikimedia Commons, and NASA searches do not require API keys.

Speechify is the primary voice-over provider when `SPVO_API` is available. It uses the `simba-3.5` model and automatically searches multiple pages of the available male voice catalog for calm/deep/baritone-style metadata. Set `SPEECHIFY_VOICE_ID` to pin a specific voice. A workspace key can be authorised for speech synthesis while `GET /v1/voices` answers `403 insufficient_scope`; in that case the catalog is skipped (the key is not wrong) and the documented, live-verified shared voice `geffen_32` is used, overridable with `SPEECHIFY_FALLBACK_VOICE_ID`. A voice id rejected by the speech endpoint rotates to the next candidate instead of ending the run. The narration uses a lower pitch and restrained pace, with subtle variation for hooks, questions, and the final line. A Speechify failure stops the run instead of silently substituting another engine; Edge TTS is used only when Speechify is not configured, or when `ALLOW_VOICE_FALLBACK=1` is set for a local test. `output/voiceover_manifest.json` records the provider, voice, model, language and every sentence duration that were actually used.

**Important Cloudflare Vision setup:** the default visual model is `@cf/meta/llama-3.2-11b-vision-instruct`. Cloudflare may require a one-time acceptance of the model's Meta license/AUP. The automation does not accept legal terms on the user's behalf. If Cloudflare rejects the first vision request because that model has not been enabled, accept the required terms in the Cloudflare dashboard or through the provider's official flow, then rerun. Cloudflare text generation can still be used separately if enabled.

**GitHub Actions secret mapping:** repository secrets are not automatically injected into Python. Add these mappings to the existing workflow job's `env:` section (preserve all current entries and do not replace your permanent-apply workflow):

```yaml
CLOUDFLARE_API_TOKEN: ${{ secrets.CLOUDFLARE_API_TOKEN }}
CLOUDFLARE_ACCOUNT_ID: ${{ secrets.CLOUDFLARE_ACCOUNT_ID }}
CF_API_TK1: ${{ secrets.CF_API_TK1 }}
CF_ID1: ${{ secrets.CF_ID1 }}
SPVO_API: ${{ secrets.SPVO_API }}
```

Map any numbered backup keys the same way, for example `GROQ_API_KEY_1: ${{ secrets.GROQ_API_KEY_1 }}`. Do not paste secret values into source files, logs, or workflow text.

## Version 10.15 — quota-aware API routing

The provider selector now ranks eligible routes using the strongest quota signal each provider actually exposes. It does not assume every API key has an independent quota, and it never fabricates a percentage for an API that does not expose remaining usage.

- **OpenRouter:** checks `GET https://openrouter.ai/api/v1/key` for each key's finite spend headroom (`limit_remaining / limit`). It also parses `error.metadata.limit_source` from quota responses and routes each limit to its real scope: `openrouter_key_limit` blocks only that key; free-tier RPM blocks all free-model routes sharing the account for the retry window; free-tier daily exhaustion blocks those free-model routes for the rest of the current workflow run; `upstream_provider_shared_pool` cools only that model route; and account credits/in-flight limits apply to the shared account pool. The free-tier request count itself is not guessed when the API does not expose a live remaining value.
- **Groq:** reads the documented `x-ratelimit-remaining-requests`, `x-ratelimit-limit-requests`, `x-ratelimit-remaining-tokens`, and `x-ratelimit-limit-tokens` response headers. The smaller remaining ratio is used. Since Groq limits are organization-scoped, all keys share one pool by default; use distinct `GROQ_QUOTA_GROUP_1`, `GROQ_QUOTA_GROUP_2`, etc. only when keys are known to belong to different organizations.
- **Gemini:** quotas are project-scoped, not key-scoped. Without a project identifier, configured Gemini keys are treated conservatively as one shared unknown project pool. Optional `GEMINI_PROJECT_ID` or `GEMINI_PROJECT_ID_1`, `GEMINI_PROJECT_ID_2`, etc. can identify distinct projects. Quota depletion and retry hints are learned from 429 responses; AI Studio remains the source for live per-project quota dashboards.
- **Cloudflare Workers AI:** token pairs are grouped by their `CLOUDFLARE_ACCOUNT_ID`/`CF_ID` account ID. Account-scoped rate/credit errors cool down the account pool instead of pointlessly rotating another token for the same account. The public inference response does not provide a generally available live remaining-quota percentage.
- **Speechify:** the documented request-rate and concurrency budgets are account-level, not key-level. A 429 follows `Retry-After` and stops repeated calls through other keys in the same account; HTTP 402 marks the account's balance/plan as unavailable and invokes the full-script Edge TTS fallback. The Speechify Console remains the usage view; no per-key remaining-quota endpoint is assumed.
- **Unknown quota:** when a provider has no preflight quota endpoint or has not yet returned useful headers, it is labelled `UNKNOWN` and scheduled with a neutral score plus least-used/least-recently-used balancing. This is a best-available scheduler, not a claim that unknown quotas have a known percentage.
- **Fallback:** before each new planning or footage-verification request, eligible providers are ranked again. Exhausted, unauthorized, or cooling-down quota pools are skipped. Visual verification remains fail-closed if no verifier can complete a valid check.

The quota router prints provider names, measured percentages where available, and explicit `UNKNOWN` labels only. It never prints API-key values. It is in-memory for each GitHub Actions run; the provider's own quota system remains authoritative between runs. OpenRouter keys are assumed to belong to one account by default. When keys belong to different OpenRouter accounts, set `OPENROUTER_QUOTA_GROUP_1`, `OPENROUTER_QUOTA_GROUP_2`, etc. in the workflow `env:` with a distinct label for each account's key slot. Keys from the same account must use the same group label.

## Output

The pipeline runs RESEARCH → SCRIPTWRITING → FACT-CHECKING → VOICE-OVER (Speechify) → FOOTAGE DISCOVERY → DOWNLOAD → AI VISION → RENDERING → FINAL QC. It produces `documentary.mp4`, `plan.json`, `credits.txt`, `timing.json`, `research.json`, `fact_check.json`, `footage_verification.json`, `copyright_report.json`, `qc_report.json`, voice-over/caption/music files (`output/voiceover_manifest.json`, `output/vision/scene_XX/*.jpg`), and diagnostic files (`failure_report.json`, `stage_report.json`, `output/pipeline.log`).

A local end-to-end check that needs no API key is available with
`python tools/local_e2e.py e2e_run` (real Edge TTS, real render and QC; synthetic
vertical clips instead of stock footage).

## Version 10.10 — source-priority enforcement

- Enforces source-tier selection: Internet Archive → Wikimedia Commons → NASA → Pexels → Pixabay.
- Ranks relevance only within each source, so a high Pexels/Pixabay relevance score cannot outrank an available Internet Archive candidate.
- Gives Internet Archive a larger candidate-check budget than each fallback source.
- Retains the existing visual verification and download-failure fallback behavior.
- Keeps the 10.9 single-role scriptwriter master prompt unchanged and loaded at runtime.
- Does not add a second LLM/API request for fact-checking.
- Omits the outdated bundled `render.yml` so the target repository's updated permanent workflow is preserved.

## Version 10.11 — resilient footage recovery and diagnostics

- Adds dedicated alternative searches for field researchers, scientists, humidity measurements, weather stations, and environmental sensors.
- Applies stronger metadata anchors to prevent off-topic NASA Shuttle footage or generic cloud footage from entering field-research verification.
- Retries Wikimedia Commons HTTP 429 responses politely, uses a timed source cooldown, and avoids caching incomplete multi-source searches.
- Applies a minimum 30-second cooldown to rate-limited visual-verification providers before trying that provider again.
- Creates the failure and stage reports expected by the existing workflow, plus a live `output/pipeline.log` capture.
- Does not add Wikipedia as a footage provider because its videos are already found through Wikimedia Commons.

## Version 10.12 — download throttling and sparse-scene recovery

- Handles Wikimedia Commons media-download HTTP 429 separately from the Commons search API. The first throttled download places Commons into a timed cooldown, so the pipeline skips the remaining Commons URLs and continues to NASA and other lower-priority sources rather than wasting attempts on the same blocked host.
- Shares a polite request interval between Commons API searches and Wikimedia media downloads, honors `Retry-After` where supplied, and removes partial files after failed downloads.
- Triggers targeted fallback searches based on the number of candidates that remain selectable after per-source caps, not the raw metadata count. This closes the gap where dozens of metadata hits could hide a shortlist of only a few usable options.
- Expands NASA, Pexels, and Pixabay candidate caps while preserving strict source-tier priority and the overall 26-candidate limit.
- Adds distinct climate-model/data-visualization alternatives alongside field-research and environmental-instrument shots. Each candidate still must pass metadata filtering and visual verification.
- Requires forest/tree metadata for generic forest queries, reducing unrelated candidates that happen to overlap on one weak term.
- Does not weaken visual verification, disable fail-closed behavior, add Wikipedia as a separate footage source, or alter the master scriptwriter prompt.


## Version 10.13 — portrait integrity, visual relevance, and readable overlays

- Requires downloaded source clips to be genuine portrait 9:16 footage (minimum short side 640 px, with a small aspect-ratio tolerance). The check uses `ffprobe` display dimensions and honors 90-degree rotation metadata; landscape, square, 4:5, and other non-9:16 sources are rejected before visual verification and rendering.
- Prefers native 9:16 renditions when providers expose multiple resolutions, including Pexels, Pixabay, and Internet Archive. MM2 keeps a low-resolution metadata precheck but defers authoritative aspect-ratio validation to the downloaded file's `ffprobe` display dimensions, including 90-degree rotation.
- Removes the fill-and-center-crop rendering path. Approved clips are fit to the 1080×1920 canvas without cropping, and the render stage independently rechecks the source ratio as a second guard.
- Makes scene-primary visual intent mandatory across fallback searches. A broad query such as “tropical forest” can no longer erase the requested aerial canopy, jaguar, raindrop-on-window, or other defining shot. The vision prompt explicitly rejects visually similar but unrelated phenomena such as volcanic ash for a moisture-plume scene.
- Reflows the opening hook over multiple centered lines, calculates a safer font size for longer lines, and no longer cuts the hook at an arbitrary 90-character limit.
- Builds subtitle groups sentence by sentence, restores terminal punctuation where the TTS boundary payload omits it, balances a dangling one-word final caption where practical, and applies one consistent caption style and line-width limit.
- Keeps `SCRIPTWRITER_MASTER_PROMPT.md` unchanged and does not add another fact-check request or new footage provider.
- Validation performed: Python syntax compilation; source-ratio accept/reject checks using synthetic 720×1280, 1280×720, and 1080×1350 samples; primary-query metadata rejection tests; sentence-bounded caption grouping; and a local FFmpeg render of a native 9:16 sample. A complete online production run still requires the configured provider/API credentials and live source availability.


## Version 10.14 — key rotation, Speechify voice-over, and duration rules

- Automatically discovers base and numbered API-key secrets for Groq, OpenRouter, Gemini, and Cloudflare, then rotates to the next configured credential after quota, billing, authentication, or transient provider failures.
- Adds Cloudflare Workers AI as an extra text-generation and visual-verification provider. Visual verification remains fail-closed; the pipeline does not bypass footage review when all providers are exhausted.
- Uses Speechify `simba-3.5` as the primary voice-over provider when `SPVO_API` is configured. The voice selector prefers calm/deep/baritone-style male voice metadata, applies a lower-pitched calm baseline, and varies delivery subtly around hooks, questions, and the ending. Edge TTS remains a complete-script fallback.
- Enforces no narration length at all: there is no minimum and no maximum word count. The only length rule is the measured voice-over — at least 70 seconds of actual Speechify audio — and the script is deepened with sourced detail and re-spoken until it reaches it.
- Requires a closing call to action as the final spoken sentence: the viewer is asked to follow the channel and to comment. It is validated in the script and in the generated audio, so a voice-over without it cannot pass.
- Enforces a minimum final video duration of 70 seconds. If the complete VO timeline is longer than 70 seconds, the video continues to the end of that VO. There is no 90-second maximum; short timelines receive only enough end silence to reach the 70-second minimum.
- Checks final rendered duration and fails if the video ends before the complete VO timeline or falls below the 70-second minimum.


## Version 10.15 — quota-aware API routing

- Adds `quota_router.py` to inspect OpenRouter per-key spend headroom, parse OpenRouter `limit_source` error metadata, parse Groq remaining-request/token headers, and track each provider's true quota scope.
- Re-ranks providers before each planning and visual-verification attempt instead of walking a fixed provider/key list. Known remaining capacity ranks above unknown capacity; unknown routes are balanced by least-used/least-recently-used scheduling without inventing percentages.
- Distinguishes OpenRouter key spending caps, account-level free-model RPM/RPD limits, model-specific upstream shared-pool throttles, and account-level credits/in-flight budgets. It no longer wastes retries on sibling keys when the error says the whole account's free-model budget is exhausted.
- Treats Groq limits as organization-scoped, Gemini limits as project-scoped, Cloudflare Workers AI limits as account-scoped, and Speechify rate/concurrency limits as account-scoped. Same-pool keys are not falsely counted as independent quota pools.
- Supports optional numbered `OPENROUTER_QUOTA_GROUP_n` labels for keys belonging to different OpenRouter accounts; `GEMINI_PROJECT_ID_n` and `GROQ_QUOTA_GROUP_n` remain available for genuinely separate projects/organizations.
- Keeps Speechify as primary VO, with Edge TTS as a full-script fallback when Speechify account-level quota/balance or service availability prevents completion.


## MM1 — smart footage discovery

- Uses metadata title, tags, description, scene intent, and shot-type anchors as separate scoring signals.
- Lets a more relevant candidate outrank source preference; trusted open archives break close-score ties.
- Rejects unrequested military, industrial, agricultural, sports, urban, winter, and other conflicting contexts before download/visual review.
- Keeps ambiguous tree/forest-only candidates in the relaxed queue, where they must pass downloaded-video visual verification.
- Adds one bounded rejection-guided query retry per scene (up to four new search queries and twelve new candidate checks).
- Applies timed source cooldowns after 429, 5xx, and network failures; avoids immediate repeat hits against Wikimedia Commons after HTTP 429.
- Keeps Pixabay/Pexels disabled and preserves strict native 9:16 validation and fail-closed visual verification.

The project ZIP filename is intentionally not used by the GitHub extraction step. The workflow selects the newest ZIP whose contents contain the required project markers, so `mm1.zip`, `mm2.zip`, `mm3.zip`, and later names are supported without changing the extraction script.


## MM2 — display-orientation-aware footage discovery

- Stops discarding search candidates solely because catalog metadata reports landscape-coded width/height; some providers omit 90-degree display-rotation metadata from search results.
- Keeps early minimum-resolution filtering and uses known native-portrait dimensions as a ranking hint within the same relevance band.
- Uses the downloaded file's `ffprobe` display dimensions and sample-aspect-ratio data as the final orientation authority. True landscape, square, 4:5, and other non-native 9:16 footage remains rejected; no crop-based workaround is introduced.
- Keeps title/tag/context ranking, visual verification, rejection-guided retries, and 429/5xx source cooldowns intact.
- CI now runs the complete discovered unittest suite instead of only quota-router tests.
- Replaces the old hard-coded standalone Pixabay demo with an inactive compatibility shim. Pixabay/Pexels remain disabled by default.


## MM3 — persistent source rate-limit protection

- Persists provider cooldowns in `footage_source_state.json` and `.api-cache/footage_source_state.json`. The root state file contains only source names, cooldown timestamps, HTTP status and short reason labels—never API keys, topics, candidate URLs or user data. The existing `persist_generated_markdown.py` always-run workflow step commits this state alongside eligible Markdown deletions, including after a failed render.
- Reads both numeric and HTTP-date `Retry-After` values and retains long provider reset windows (up to a defensive seven-day ceiling). HTTP 429 is not immediately retried; the affected source is skipped until its saved cooldown expires.
- Caches each provider's search results independently. A Wikimedia 429 no longer prevents successful Internet Archive or NASA results from being cached. Successful empty searches are cached too, avoiding repeated no-hit requests. Normal cache TTLs are six hours for Internet Archive and Wikimedia, twelve hours for NASA, and three hours for Google Search.
- Uses a stale per-source result cache for up to seven days when a provider is cooling down or a refresh fails. Stale candidates still pass the same metadata, downloaded-file native 9:16, license and visual-verification checks before selection.
- Adds source-specific request pacing and per-run request ceilings for Internet Archive, Wikimedia Commons, NASA and Google Search. Pacing applies to discovery, metadata/manifest requests and downloads. Request ceilings are safety limits, not goals.
- Caches NASA asset manifests for thirty days and keeps the existing seven-day Internet Archive item-metadata cache.
- Deduplicates candidate URLs across providers before ranking. Source cooldowns reset only after their saved expiry; missing or corrupt cache/state files remain non-fatal.
- Does not add providers, turn on Pixabay/Pexels, weaken footage relevance, lower the visual-confidence threshold, or accept non-native 9:16 footage.

**Workflow note:** no YML replacement is required on the current `main` workflow because its existing final `Persist generated Markdown cleanup to this branch` step already runs with `if: always()` and calls `persist_generated_markdown.py`. MM3 extends that existing helper to commit the small cooldown-state file. Keep the current workflow and upload `mm3.zip` as the project archive.
