# Auto Documentary Video Generator — PROJECT1

Automated vertical mini-documentary generator using AI-assisted story planning, licensed stock footage and published institutional visuals, Edge TTS narration, word-synchronized captions, and FFmpeg rendering.

## Output

- `documentary.mp4` — 1080 × 1920, 30 FPS, H.264/AAC.
- `plan.json` — script, scene plan, selected media and provenance metadata.
- `credits.txt` — asset creator, source page, license/usage information, and visual-verification status.
- `production_report.json` — per-scene source, mandatory pixel-verification results, reference visual details, opening layout, voice-over QA, scene timing, subtitle-alignment diagnostics, and the complete narrative fact-check report.
- `fact_check_report.json` — fetched trusted-source pages, cited narration sentences, independent fact-check verdicts, and evidence excerpts. It is written in the Actions runner workspace; the current workflow uploads the complete embedded copy inside `production_report.json`, not this file separately.
- `voiceover_manifest.json` — validated VO duration, audio-stream/loudness checks, voice settings, VO-anchored scene windows, and measured-versus-estimated word-timing coverage. The complete manifest is embedded in the uploaded `production_report.json`.
- `timing.json` — sentence events, word-level timings, timing source for every word, and caption phrase groups.
- `output/voiceover.mp3` and `output/captions.ass` — final voice-over and VO-synchronized subtitle file.

## GitHub Actions setup

Switch the repository branch selector to `project1` before uploading the upgrade ZIP or running **Actions → Hybrid Documentary Generator → Run workflow**. The self-upgrade workflow commits to the currently selected branch. Do not run it from `main`.

Upload the file `auto-documentary-video-upgrade.zip` to the repository root, commit it, and run the workflow with branch `project1`. Enter the documentary topic in the workflow input.

Optional GitHub Actions secrets:

- `GROQ_API_KEY`
- `DEEPSEEK_API_KEY`
- `GEMINI_API_KEY`
- `OPENROUTER_API_KEY`
- `PIXABAY_API_KEY`
- `PEXELS_API_KEY`
- `COVERR_API_KEY`

Text, fact-check, and visual AI providers depend on valid keys, model availability, service status, and quota. The factual review uses its own provider order/cooldown (`AI_FACT_PROVIDER_ORDER`, defaulting to DeepSeek → Gemini → Groq → OpenRouter) so a script-writing provider outage does not automatically disable the fact-review route. Stock-footage providers are independently disabled for the remainder of a run after authentication, permission, or rate-limit errors (HTTP 401, 403, or 429). For text/fact generation, Groq, DeepSeek, and OpenRouter explicitly request JSON mode; a malformed or validator-rejected response receives one targeted repair attempt on the same provider before failover. HTTP 402 billing errors are reported separately and skip that provider for the current run. This improves recovery but cannot replenish a provider balance or guarantee service availability.

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
3. Fetches the source page directly, strips scripts and non-visible elements, and selects claim-relevant text from the actual page. Model-written summaries, page titles, and unrelated snippets are not accepted as evidence.
4. Sends the complete script plus fetched excerpts to a separate fact-audit route. Every sentence must be directly supported, and the auditor must report any factual assertion not covered by the ledger.
5. Requires quantitative claims containing numbers or measurements to cite at least two distinct source organisations. Two subdomains of the same organisation do not count as independent evidence, and the fact auditor must confirm both.
6. Applies limits to claim count, URLs, redirects, and page size. A source page without a relevant passage blocks the narration.

A failed, inaccessible, contradicted, or insufficient source blocks the script before TTS/render. The system does not substitute a generic unaudited local script when planning fails. This is deliberately fail-closed: some topics or provider outages can stop a run rather than allowing unsupported claims into a documentary. AI-assisted evidence review materially reduces hallucination risk but cannot mathematically guarantee truth. Review the fetched excerpts and verdicts in `production_report.json` before publishing.

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
