# Auto Documentary Video — Version 10.15

Automated vertical documentary generator. It builds a scene plan, searches for relevant real-world footage, performs visual verification, generates narration and captions, and renders the final video with FFmpeg.

## Master scriptwriter prompt

`SCRIPTWRITER_MASTER_PROMPT.md` remains the single source of truth for the generator's professional documentary scriptwriter role. `main.py` loads it at runtime through `documentary_prompt()`. The prompt enforces factual discipline, an eight-scene structure, a strict 120–200-word narration boundary, and practical footage queries. Its claim-by-claim audit is part of the same prompt; it does not create a second independent fact-check API call.

## Footage source selection — version 10.12

Version 10.13 adds strict source-orientation and scene-relevance gates on top of the existing provider priority.

The active pipeline (`render.py` → `main.py`) enforces this **selection** priority:

1. Internet Archive
2. Wikimedia Commons
3. NASA
4. Pexels
5. Pixabay

Candidate relevance is ranked **within each source tier**, never globally across all providers. The default total visual-check budget is up to 26 candidates. Per-source caps are 10 for Internet Archive, 4 for Wikimedia Commons, and 8 each for NASA, Pexels, and Pixabay; the total cap and source availability still apply. The generator attempts the Internet Archive tier first; it proceeds to Wikimedia Commons, NASA, Pexels, and Pixabay only when candidates in earlier tiers fail to download or fail visual verification.

Important distinction: the existing search collector still gathers metadata from configured sources before candidate selection. This upgrade enforces which source's clips are attempted first; it does not yet make fallback API searches lazy. Pexels and Pixabay are fallback providers, not the top-ranked selection source.

Internet Archive results are admitted only when metadata explicitly indicates CC0, CC BY, or Public Domain. Wikimedia Commons results are license-filtered. NASA assets may require checking third-party credits. Always verify the exact source-page license and attribution requirements before publishing.

`footage.py` is a legacy standalone Pixabay utility and is not called by the active `render.py` → `main.py` pipeline. Use the integrated pipeline for documentary production.

## GitHub Actions workflow preservation

This ZIP intentionally **does not include** `.github/workflows/render.yml`. The bundled 10.9 workflow is older and would risk overwriting the newer permanent-apply workflow already configured in the target repository. Keep the updated workflow on your branch; this archive only overlays the project files it contains and does not alter that workflow.

## Required GitHub Secrets

Add one or more AI provider credentials. The pipeline automatically discovers numbered keys such as `GROQ_API_KEY`, `GROQ_API_KEY_1`, `GROQ_API_KEY_2`; the same convention works for `OPENROUTER_API_KEY` and `GEMINI_API_KEY`.

Cloudflare Workers AI supports paired credentials: `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID`, and the additional aliases `CF_API_TK1` + `CF_ID1` (or numbered pairs 2, 3, etc.). It is included as an additional text and visual-verification fallback. `PIXABAY_API_KEY` and `PEXELS_API_KEY` remain optional footage-source keys. Internet Archive, Wikimedia Commons, and NASA searches do not require API keys.

Speechify is the primary voice-over provider when `SPVO_API` is available. It uses the `simba-3.5` model and automatically searches multiple pages of the available male voice catalog for calm/deep/baritone-style metadata. Set `SPEECHIFY_VOICE_ID` to pin a specific voice. The narration uses a lower pitch and restrained pace, with subtle variation for hooks, questions, and the final line. Existing Edge TTS remains a full-script backup if Speechify is unavailable or its account-level quota is exhausted.

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

The pipeline produces `documentary.mp4`, `plan.json`, `credits.txt`, `timing.json`, voice-over/caption files, and diagnostic files (`failure_report.json`, `stage_report.json`, `output/pipeline.log`) when a run fails.

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
- Prefers native 9:16 renditions when providers expose multiple resolutions, including Pexels, Pixabay, and Internet Archive. Provider metadata dimensions are only an early filter; the actual downloaded file is always probed before acceptance.
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
- Enforces a script size of 120–200 spoken words.
- Enforces a minimum final video duration of 70 seconds. If the complete VO timeline is longer than 70 seconds, the video continues to the end of that VO. There is no 90-second maximum; short timelines receive only enough end silence to reach the 70-second minimum.
- Checks final rendered duration and fails if the video ends before the complete VO timeline or falls below the 70-second minimum.


## Version 10.15 — quota-aware API routing

- Adds `quota_router.py` to inspect OpenRouter per-key spend headroom, parse OpenRouter `limit_source` error metadata, parse Groq remaining-request/token headers, and track each provider's true quota scope.
- Re-ranks providers before each planning and visual-verification attempt instead of walking a fixed provider/key list. Known remaining capacity ranks above unknown capacity; unknown routes are balanced by least-used/least-recently-used scheduling without inventing percentages.
- Distinguishes OpenRouter key spending caps, account-level free-model RPM/RPD limits, model-specific upstream shared-pool throttles, and account-level credits/in-flight budgets. It no longer wastes retries on sibling keys when the error says the whole account's free-model budget is exhausted.
- Treats Groq limits as organization-scoped, Gemini limits as project-scoped, Cloudflare Workers AI limits as account-scoped, and Speechify rate/concurrency limits as account-scoped. Same-pool keys are not falsely counted as independent quota pools.
- Supports optional numbered `OPENROUTER_QUOTA_GROUP_n` labels for keys belonging to different OpenRouter accounts; `GEMINI_PROJECT_ID_n` and `GROQ_QUOTA_GROUP_n` remain available for genuinely separate projects/organizations.
- Keeps Speechify as primary VO, with Edge TTS as a full-script fallback when Speechify account-level quota/balance or service availability prevents completion.
