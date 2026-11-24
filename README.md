# Auto Documentary Video Generator — PROJECT1

Automated vertical mini-documentary generator using AI-assisted story planning, licensed stock footage and published institutional visuals, Edge TTS narration, word-synchronized captions, and FFmpeg rendering.

## Output

- `documentary.mp4` — 1080 × 1920, 30 FPS, H.264/AAC.
- `plan.json` — script, scene plan, selected media and provenance metadata.
- `credits.txt` — asset creator, source page, license/usage information, and visual-verification status.
- `production_report.json` — per-scene source, mandatory pixel-verification results, reference visual details, opening layout, voice-over QA, scene timing, subtitle-alignment diagnostics, and the complete narrative fact-check report.
- `fact_check_report.json` — fetched trusted-source pages, cited narration sentences, independent fact-check verdicts, and evidence excerpts. Uploaded both on success and on failure.
- `stage_report.json` — per-stage timings, status, and a runtime/environment summary. Written for every run, successful or not.
- `failure_report.json` — written only when a run stops early: the failing stage, error type/message, redacted traceback, and the stage timeline. Credentials are scrubbed before anything is written.
- `opening_layout.json` — measured opening title/hook card layout that was validated before narration was generated.
- `voiceover_manifest.json` — validated VO duration, audio-stream/loudness checks, voice settings, VO-anchored scene windows, and measured-versus-estimated word-timing coverage. The complete manifest is embedded in the uploaded `production_report.json`.
- `timing.json` — sentence events, word-level timings, timing source for every word, and caption phrase groups.
- `output/voiceover.mp3` and `output/captions.ass` — final voice-over and VO-synchronized subtitle file.

## GitHub Actions setup

Switch the repository branch selector to `project1` before running **Actions → Hybrid Documentary Generator → Run workflow**. Do not run it from `main`.

A bundled upgrade ZIP in the repository root (for example `project1-10.7.zip`) is unpacked into the runner workspace for that run only. The workflow never commits or pushes: a render job must not mutate the branch it is building from.

On failure the job uploads a `documentary-diagnostics-<run>` artifact with `failure_report.json`, `stage_report.json`, `output/pipeline.log`, every report written so far and any partial media, then prints the failing stage and the last 120 log lines in the job summary. Both success and failure artifacts are configured, so a red run is diagnosable without re-running it.

Optional GitHub Actions secrets:

- `GROQ_API_KEY`
- `DEEPSEEK_API_KEY`
- `GEMINI_API_KEY`
- `OPENROUTER_API_KEY`
- `PIXABAY_API_KEY`
- `PEXELS_API_KEY`
- `COVERR_API_KEY`

### Running this for free

Nothing in this project requires a paid subscription. The whole pipeline can run on free tiers and free software:

| Part | Cost | What you need |
| --- | --- | --- |
| Script writing and fact review | Free tier | One key from Groq (https://console.groq.com), Google AI Studio Gemini (https://aistudio.google.com/apikey), or OpenRouter free models (https://openrouter.ai/keys) |
| Vision verification | Free tier | The same Groq / Gemini / OpenRouter key; OpenRouter defaults to `:free` vision models |
| Stock footage | Free | Pexels (https://www.pexels.com/api/), Pixabay (https://pixabay.com/api/docs/) and Coverr keys are free to create; NASA and Wikimedia need no key |
| Narration | Free | Edge TTS, no account |
| Rendering | Free | FFmpeg runs on the runner; the encoding itself costs nothing |
| GitHub Actions | Free minutes | Public repositories run free; private repositories get a monthly free minute allowance |

`DEEPSEEK_API_KEY` is the only prepaid-balance provider here, and paid OpenRouter slugs can bill as well. Both are excluded by default: `AI_ALLOW_PAID_PROVIDERS` is `false`, so the router logs that it skipped them and continues with free-tier routes only. DeepSeek is not even part of the default provider order. A prepaid key sitting in GitHub Secrets therefore cannot be charged by a normal run. The startup log states the active cost policy, and the run stops with a clear error if no free-tier route succeeds — it never silently falls back to a paid one.

OpenRouter is additionally filtered by model slug in free mode: only `openrouter/free` and `*:free` slugs are registered, so a paid slug configured by mistake is skipped with a log line. Gemini model IDs change between releases; a retired ID returning 404 is retried against `GEMINI_MODEL_FALLBACKS` (`gemini-3.5-flash-lite` → `gemini-3.8-flash` → `gemini-2.5-flash` → `gemini-2.0-flash`) instead of failing the run.

No keys at all and no money: `python scripts/offline_render_check.py` renders a real 1080 × 1920 MP4 with free Edge TTS and locally generated clips, which verifies encoding, muxing, captions and duration alignment without any API call.

### Agent-driven runs (`ci-run` branch)

A push to the `ci-run` branch starts the same pipeline with the topic in `ci-topic.txt`, without using the Run-workflow form. Only that branch publishes `stage_report.json`, `failure_report.json`, `plan.json`, the fact-check report and a trimmed `pipeline.log` to the `ci-diagnostics` branch, so a failed run can be inspected without downloading artifacts. The job never commits to `project1` or `main`.

A 429 pauses a provider for a bounded window (honouring `Retry-After`, default 180 s, `AI_RATE_LIMIT_COOLDOWN_SECONDS`) instead of deleting it for the whole run; only an explicitly exhausted quota, billing or authentication failure disables a provider. DeepSeek's chat endpoint is text-only, so it is only used for vision when `DEEPSEEK_VISION_MODEL` is set explicitly.

### Provider retries (Phase 1)

Every recoverable failure keeps its route alive for a later pass instead of removing it from the run:

- `AI_ROUTER_MAX_PASSES` (default 2, the workflow uses 4) full sweeps over the provider order, separated by exponential backoff: `AI_RETRY_BASE_SECONDS` 5 s doubling up to `AI_RETRY_MAX_SECONDS` 60 s.
- A wait the provider declares itself (`Retry-After`, or an exhausted tokens-per-minute window of `AI_TPM_WINDOW_SECONDS` 60 s) is only slept for when it fits inside `AI_ROUTER_WAIT_BUDGET_SECONDS` (default 0 for library callers; the workflow allows 600 s). Otherwise the router fails over immediately and never blocks on a window it cannot outlast.
- HTTP 413 (request larger than the free-tier `AI_TEXT_TOKEN_CEILING`, 8,000 tokens) shrinks the completion budget and retries the same route; the Groq budget is now sized from the actual prompt so prompt plus budget fits the ceiling.
- An empty answer with `finish_reason=length`, or an incomplete JSON object, is treated as a truncated output: the completion budget shrinks and the same route is retried. It no longer disables the provider.
- A 400 whose message concerns JSON/token validation (`Failed to validate JSON`) is treated as an output-side problem with a short cooldown, not a broken configuration.
- Provider health is recorded per pipeline sub-stage (`generate_json(..., stage=...)`), so a 503 while building the source pool no longer cools the same provider down for script generation and fact checking minutes later.

Text, fact-check, and visual AI providers depend on valid keys, model availability, service status, and quota. The factual review uses its own provider order/cooldown (`AI_FACT_PROVIDER_ORDER`, defaulting to Gemini → Groq → OpenRouter → DeepSeek) so a script-writing provider outage does not automatically disable the fact-review route, and free-tier routes are exhausted before the prepaid one. Stock-footage providers are independently disabled for the remainder of a run after authentication, permission, or rate-limit errors (HTTP 401, 403, or 429). For text/fact generation, Groq, DeepSeek, and OpenRouter explicitly request JSON mode; a malformed or validator-rejected response receives one targeted repair attempt on the same provider before failover. HTTP 402 billing errors are reported separately and skip that provider for the current run. This improves recovery but cannot replenish a provider balance or guarantee service availability.

## Visual sourcing policy

The production path does **not** create procedural scientific diagrams, charts, tables, maps, anatomy plates, or data graphics. Real footage is the default, including for filmable natural processes such as rainfall, evaporation, transpiration, cloud formation, and river flow. A published reference visual is requested only when the narration needs measurable/geographic evidence or an internal scientific structure that ordinary footage cannot faithfully show. The generator searches NASA's image library and Wikimedia Commons records that include a linked primary institutional source and an accepted reusable license.

- NASA assets explicitly marked as third-party copyrighted/restricted are skipped. NASA media usage rules still apply; the source is credited and no endorsement is implied.
- Commons images are accepted only when a linked primary source belongs to a recognised institutional domain and the file has a reusable license such as CC0, public domain, CC BY, or CC BY-SA. Attribution and license details are included in `credits.txt`.
- The published image's scientific content is preserved. It is fitted into the vertical canvas without cropping its data or labels; the frame adds only the title and source credit.
- If no suitable published visual is found, the generator tries relevant real footage. It does not invent a replacement diagram. If no acceptable footage or published visual exists, the run stops with a diagnostic rather than silently presenting an unrelated illustration.
- The legacy `visual_fallback.py` module remains in the repository for compatibility and its unit tests, but the production `main.py` path does not call it.

The local SigLIP model ranks stock-footage thumbnails before external visual verification; it is not represented as a per-frame AI verdict. **Every production scene must pass pixel-level AI Vision verification at a hard minimum confidence of 0.72. Environment settings may make this stricter but cannot lower it. Metadata-only acceptance is disabled.** When a provider returns a genuine REJECT, the candidate is discarded. When all Vision providers are unavailable, candidates are discarded and the run stops if no verified alternative remains; the system never relaxes the quality gate to force a render. Published reference visuals must also pass pixel-level verification. Each scene includes a required `visual_focus`, and the verifier checks both the visible focus and its relevance to the complete scene narration. Exact duplicate video files are also blocked across scenes using SHA-256 content hashes. Reference-artifact words (such as map/chart/diagram) are removed from stock-footage queries so the stock search looks for filmable subjects instead of still graphics.

## Evidence-backed narrative fact checking

Before voice-over generation or footage downloads, the generator now:

1. Requires every narration sentence to appear verbatim in `fact_claims`, linked to one or more source-page URLs. No sentence can bypass the evidence ledger.
2. Accepts only HTTPS pages on a trusted institutional allowlist (government, university, international institutions, selected scientific publishers and established reference institutions). Each redirect is checked before it is followed; redirects outside the allowlist are rejected without making the redirected request.
3. Fetches the source page directly, strips scripts and non-visible elements, and selects claim-relevant text from the actual page. A page that opens but does not share the claim's subject terms is rejected, and a numeric claim additionally requires the claimed value and unit in the excerpt. Model-written summaries, page titles, and unrelated snippets are not accepted as evidence.
4. Sends the complete script plus fetched excerpts to a separate fact-audit route. Every sentence must be directly supported, and the auditor must report any factual assertion not covered by the ledger.
5. Requires quantitative claims containing numbers or measurements to be supported by at least two distinct source organisations. Two subdomains of the same organisation do not count as independent evidence, and the fact auditor must confirm both.
6. If the planner's initial pages do not yield enough relevant evidence, one bounded source-recovery call may request up to two additional direct pages from organisations not already tried. These URLs are only candidates: each must pass the HTTPS allowlist, safe-redirect checks, direct fetch, and claim-relevant excerpt selection before it can count as evidence. This recovery also runs when a non-quantitative claim has no usable initial page.
7. Applies limits to claim count, total pages, recovery attempts, redirects, and page size. If recovery cannot produce enough relevant independent evidence, narration remains blocked.

### Fact-check grounding (Phase 2)

The archived failure (`C4`: "up to 75% of regional rainfall originates from forest-generated moisture") happened because source discovery returned no candidates while some NCBI pages were still counted as "usable". The verifier now separates *page opened* from *page is evidence*:

- **Claim-level relevance gate.** A fetched page must share at least two of the claim's content terms (one for a one-term claim) before it can become an excerpt. A page about a different subject is reported as `Fetched page is about a different subject` instead of being passed to the auditor.
- **Numeric grounding.** A claim with a number, percentage or measurement must be evidenced by a passage that states that exact value **with its unit**: `75%` is not satisfied by "75th percentile". The matching sentence is forced into the excerpt.
- **Scientific evidence extraction.** Publisher `citation_abstract`/`description` metadata is treated as page text, and JATS XML from the free NCBI E-utilities endpoint is extracted (the `pmc.ncbi.nlm.nih.gov` HTML page is a reCAPTCHA challenge, so it is never mistaken for evidence). Anti-bot challenge pages are rejected with a clear diagnostic.
- **Independent discovery fallback.** Beyond Wikipedia-cited pages, the verifier queries the keyless Europe PMC REST API for open-access full texts, and still fetches and gates every candidate exactly like a planner URL.
- **Blocked hosts are not retried.** A host that answered 401/403 once in a run (for example Britannica) is skipped with a recorded reason instead of being re-requested for every claim.
- **Planner repair instead of a dead run.** When no fetched page can support one sentence, the planner route is asked to rewrite that sentence strictly from the fetched passages (or remove it) and to cite verified URLs. The rewritten claim is then fetched, gated and audited from scratch. At most `MAX_CLAIM_REPAIRS` (2) repairs are allowed, the narration must stay 150-195 words, and an invented URL or a repeated unsupported sentence rejects the repair. If repairs are exhausted the run still fails closed.
- **Stage-scoped fact routing.** Fact-audit, source-recovery and repair calls pass `stage=` so a Gemini rate limit during recovery cannot cool down the audit route.

A failed, inaccessible, contradicted, or insufficient claim blocks the script before TTS/render unless the planner can replace it with an evidence-backed sentence. The system does not substitute a generic unaudited local script when planning fails. This is deliberately fail-closed: some topics or provider outages can stop a run rather than allowing unsupported claims into a documentary. AI-assisted evidence review materially reduces hallucination risk but cannot mathematically guarantee truth. Review the fetched excerpts and verdicts in `production_report.json` before publishing.

## Opening title and hook

The opening title/hook is rendered as a separate transparent Pillow overlay. Text is wrapped using actual pixel widths, preserves Unicode punctuation (including curly apostrophes), and is checked before rendering. The first scene is forced to use moving footage so the opening card is always available. The build fails clearly if supplied title/hook text cannot fit safely; it does not silently clip words at the screen edge.

## Production order: script → voice-over → everything else

The pipeline now has a strict master-reference order:

1. Generate and validate the complete script and its eight scene passages, single-focus visual plans, and exact-quote fact ledger.
2. Fetch trusted source pages and independently audit every factual assertion; stop before TTS if the evidence is missing or insufficient.
3. Generate the complete voice-over audio independently of subtitles and footage.
4. Validate the actual VO file with ffprobe and FFmpeg loudness analysis (audio stream, sample rate, duration, mean level, and peak level). If the audio fails, the run stops before footage search or visual API work.
5. Lock the measured VO sentence timeline as the master timing reference and attach scene start/end windows to it.
6. Build subtitles from the final VO. Edge TTS word boundaries are used where present; omitted events are estimated only inside the measured sentence audio duration, using adjacent reliable timings and word-length/syllable weighting. No script word is dropped. The timing report shows measured and estimated word counts.
7. Search for scene visuals against the already-locked narration map, enforce actual pixel verification for every selected visual, then render cuts and title/overlay timing to that same timeline.

- Captions are grouped by punctuation, clause boundaries, timing gaps, and phrase length—not fixed word counts.
- Each phrase uses word-level ASS karaoke timing, highlighting the current spoken word.
- Small VO duration deviations may be corrected with pitch-preserving FFmpeg `atempo`; all scene, sentence, and word timestamps are scaled by the measured duration change. Large deviations are rejected rather than forcing unnatural speech.
- Technical audio checks verify that the VO file is present, non-silent, has a supported sample rate, and does not exceed the clipping threshold. They cannot replace a human listening review for pronunciation, voice character, or subjective naturalness; the MP3 is included in the GitHub artifact for that review.

## Offline render verification

```bash
python scripts/offline_render_check.py --preset medium
```

This runs the real production functions — `build_voiceover` (free Edge TTS when reachable), `build_subtitles_from_voice`, `attach_voice_timing_to_scenes`, `render` and `validate_final_video` — against real FFmpeg at 1080 × 1920 with locally generated placeholder clips, so it makes **no paid API calls** and needs no stock-footage keys. It writes `output/offline_render_evidence.json` with ffprobe measurements for the picture track, audio track, duration delta, subtitle alignment coverage and render time.

It verifies encoding, muxing, caption burn-in and duration alignment. It does **not** verify footage relevance, visual verification, or fact-checking, which need the real provider keys; those run only in the workflow.

Useful environment overrides: `FFMPEG_BIN`/`FFPROBE_BIN` for a bundled FFmpeg build, `HOOK_FONT_PATH`/`HOOK_FONT_BOLD_PATH` to force a specific TrueType font for the opening card (DejaVu Sans is bundled in `assets/fonts`), and `AI_RATE_LIMIT_COOLDOWN_SECONDS`/`AI_TRANSIENT_COOLDOWN_SECONDS` for provider pause lengths.

## Local setup

Requirements: Python 3.11+, FFmpeg/ffprobe with libass subtitles, and packages in `requirements.txt`.

```bash
python -m pip install -r requirements.txt
python main.py
```

Set provider keys as environment variables before running locally.

## Important notes

- Verify the generated MP4 and `credits.txt` before publishing.
- Check each source page and any applicable license terms, especially for monetized/commercial publishing.
- Review the fetched evidence excerpts and independent verdict in `production_report.json` before publishing; AI-assisted verification reduces hallucination risk but cannot replace editorial judgement.
- Provider availability, quotas, and topic-specific footage coverage can affect whether all eight scenes can be completed.

## Latest routing reliability patch — Pass 10.1

The failed Actions run `122778749468` showed a Groq scene-focus validation mismatch, DeepSeek insufficient balance, Gemini HTTP 503, and an unreadable OpenRouter response. Pass 10.1 addresses the software-side causes without weakening fact or visual checks: text/fact routes use JSON mode where supported; a schema-invalid response gets one targeted retry on that same provider; OpenRouter response parsing reports specific structural problems; and HTTP 402 is clearly reported as a billing condition. The planner prompt explicitly aligns `visual_focus` wording with `visual_query` so it satisfies the existing deterministic validator. See `PASS10_1_ROUTING_VALIDATION.md` for the exact evidence and test coverage.

This cannot repair an empty DeepSeek balance or force an external provider to recover from HTTP 503. If all routes remain unavailable or produce invalid plans after the bounded repair, generation still stops rather than generating unsupported narration.

### Historical reliability notes (superseded where noted)

The following Pass 6/7/9 notes describe earlier stages of this branch. **Pass 10 supersedes the Pass 6 metadata-only Vision-outage fallback: current production never accepts a clip without pixel-level Vision verification.**

### Pass 6 reliability update — historical

Pass 6 originally expanded the candidate scan and introduced a conservative metadata-based policy for genuine Vision-service outages. That metadata-only acceptance path is no longer called by production `main.py`; a candidate without a pixel-level Vision PASS is now rejected.

### Pass 7 reliability update — search expansion retained

When the initial footage search returns fewer than five metadata-qualified candidates, the generator adds a short secondary search using broader but subject-preserving variants. Temporary visual details such as mist or camera wording no longer unnecessarily narrow every search. Alternative shots remain independent, and specific animals/plants are not dropped from their queries. At least eight candidates can be pixel-checked even when the legacy workflow environment still says five.

### Pass 9 footage-first visual planning — retained and tightened by Pass 10

Pass 9 prevents generic natural-process scenes from being forced into map/chart searches. The planner keeps `visual_query` and its alternatives focused on real footage; published maps/charts are reserved for narration that contains a concrete quantitative, geographic-distribution, or internal-structure evidence need. If a reference asset is not found, footage queries strip graphic-artifact terms and expand rainforest/rainfall searches into real shots (rain falling in forest, raindrops on leaves, and rain over canopy). Pass 10 retains this footage-first behavior while enforcing mandatory pixel verification against the full scene narration.

## Pass 10.3 — conservative scene-count recovery

If a planning model returns a near-miss scene count, the planner now attempts a deterministic, lossless repair before using the existing same-provider retry and multi-provider fallback. It may split a two-sentence scene only when both complete sentences independently name the same visual focus. It may merge adjacent one-sentence scenes only when their dominant visual subjects, footage queries, and visual modes match closely. Evidence-ledger scene indices are remapped with the narration. The repair never invents narration, deletes a scene, weakens factual checks, lowers the Vision threshold, or accepts a rejected clip. If the transformation is not clearly safe, the original strict validation and provider failover remain in force.

Local validation for this patch: Python compilation passed and all 77 unit tests passed, including four new scene-normalization regression tests. No live provider request or GitHub Actions render was performed in this environment; provider balances, rate limits, and current service availability remain external dependencies.
