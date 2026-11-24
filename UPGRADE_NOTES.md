# PROJECT1 — VO-master production pipeline upgrade (Pass 5)

## Problems observed in `revisi8`

1. The opening title/hook was one long `drawtext` line. The render cropped text at the right edge and the sanitizing regex deleted Unicode punctuation such as the curly apostrophe in `Amazon’s`.
2. Captions were assembled into fixed word-count chunks. This ignored clause boundaries and removed punctuation, so phrases could break unnaturally even though TTS word-boundary events existed.
3. When visual verification providers were unavailable, some scenes fell back to locally fabricated explanatory illustrations. Those illustrations were not authoritative evidence and could be semantically wrong.

## Changes retained from Pass 4

### Opening title/hook
- Replace one-line FFmpeg `drawtext` with a dedicated transparent Pillow overlay.
- Measure text using actual font pixels and wrap both title and hook within the safe width.
- Preserve Unicode punctuation and complete title/hook text.
- Grow the card from measured line counts; never silently truncate overflow. Stop with an explicit error if unusually long text cannot fit the safe opening area.
- Force scene 1 to use footage so the opening title/hook overlay is not skipped by a reference-image scene.
- Save the computed layout in `opening_layout.json` and include it in `production_report.json`.

### Subtitle pacing
- Align Edge TTS word-boundary events to the original script tokens to restore commas, apostrophes and sentence-ending punctuation.
- Use ordered token matching to align provider events without attaching a timestamp to the wrong repeated word.
- Group captions at sentence/clause punctuation, natural timing gaps, maximum phrase length, and phrase duration; no fixed five-word rule.
- Add ASS word-level karaoke timing (`\\kf`) using TTS offsets/durations so the highlighted word follows the spoken word.
- Prevent phrases from crossing sentence boundaries and cap events at the measured voice-over end.
- In Pass 5, missing TTS word-boundary events no longer fail the entire job. All script words remain in the captions; missing timings are estimated within the measured sentence audio window and explicitly marked in timing diagnostics.

### Published source visuals
- Remove the production-path call to procedural fallback diagrams.
- Detect maps, charts, tables, satellite images, diagrams, anatomy plates, scientific illustrations, explanatory animations, infographics, and other evidence visual requests from explicit scene visual queries/mode—not incidental terms in narration.
- Search NASA Images and Wikimedia Commons for already-published still visuals.
- Require specific metadata match and institutional provenance. Commons candidates must carry a linked primary institutional source URL; a free-text creator/credit claim is not sufficient proof of origin.
- Accept only reusable Commons licenses (CC0, public domain, CC BY, CC BY-SA); reject NonCommercial and NoDerivatives licenses.
- Skip NASA assets whose metadata indicates third-party copyright or restricted use; include NASA media-usage guidance in credits.
- Preserve published source pixels and labels; fit the full image without cropping, add source identification, and record exact source page, creator and license in `credits.txt`, `plan.json`, and `production_report.json`.
- If no suitable reference visual is found, try relevant real footage. Never draw a substitute diagram. If neither source asset nor acceptable footage can be selected, fail with a diagnostic.
- Scene 1 is always footage, ensuring the opening hook card is available.

## Tests and validation

Run:

```bash
python -m py_compile main.py ai_router.py visual_matcher.py visual_fallback.py healthcheck.py hook_overlay.py caption_timing.py reference_visuals.py source_visuals.py
python -m unittest discover -s tests -v
```

Tests cover title wrapping and Unicode punctuation, complete word preservation with missing/extra TTS events, natural phrase groups, karaoke tags, map/diagram policy, source provenance, reusable licenses, and rejection of NASA assets explicitly marked as third-party/restricted. The full GitHub Actions run and external API availability still need validation in the user's repository.

## Install

Upload this ZIP to the repository root on branch `project1` using the exact filename `project1-10.5.zip`, commit it, then run **Hybrid Documentary Generator** from branch `project1`.


## Changes in Pass 5 — voice-over is the master reference

### Strict production sequence
- Stage 1 writes and validates the complete script and confirms every planned scene maps to contiguous script sentences.
- Stage 2 generates the entire VO independently. It does not create subtitles or search for footage while narration is still being built.
- The complete WAV is assembled, pitch-preserving duration correction is applied only within the safe tempo range, and an MP3 listening copy is written.
- Before any visual-provider calls, FFmpeg/ffprobe verify the audio stream, sample rate, channel count, actual duration, mean volume, and peak volume. Missing, silent, too-quiet, corrupt, or clipped audio stops production at this gate.
- The validated VO timeline is locked and copied to every scene as `voice_timing`; scene visual windows use those measured timestamps.
- Subtitles are generated only after VO validation. Every word is preserved; exact Edge TTS timings are used when available and only missing words are estimated between reliable neighbours or within the sentence's measured audio duration.
- Visual searches then run against the already-locked scene/narration timeline. Rendering uses the same sentence and scene timing as the VO.

### Evidence and review artifacts
- `voiceover_manifest.json` records voice settings, final duration, audio QA values, scene timing windows, and subtitle alignment coverage.
- `timing.json` identifies each word as Edge TTS-timed or estimated; it also declares `validated_voiceover` as the timing master.
- `production_report.json` embeds the voice-over manifest and per-scene VO windows so the diagnostics remain available in the current Actions artifact.
- `output/voiceover.mp3` remains part of the Actions artifact so the user can listen to the actual generated VO.

### Tests added
- Missing 2 of 24 word-boundary events: retain all 24 words, preserve the order, and mark precisely 2 as estimated.
- No word-boundary events: retain every script word and distribute estimates across the measured audio duration.
- Extra provider boundary events: ignore unmatched noise without shifting timestamps onto the wrong script word.
- Integrated local TTS simulation: full VO assembly, duration correction, ffprobe/audio loudness gate, VO-anchored scene timing, and subtitle generation.

## Pass 5 validation caveat
The local integrated test uses eSpeak-generated test speech behind a fake Edge TTS interface because the real `edge-tts` package and external TTS service are not available in the isolated build environment. This verifies orchestration, media handling, timing repair, and audio QA logic; it does not certify Edge's pronunciation or naturalness. The real Edge TTS/GitHub Actions run still needs validation in the user's repository.

## Pass 6 — resilient footage selection during AI Vision outages

- Increased the default footage verification candidate scan from 5 to 8 (still capped at 8) so a weak first batch does not prematurely stop a scene.
- When every configured Vision provider is temporarily unavailable, candidate acceptance now evaluates both the original visual-search query and the actual scene narration. Strong narration-context metadata can qualify footage even when a camera/source-specific query such as “NASA Landsat satellite image” is too restrictive.
- Weak candidates remain rejected. The fallback never treats an unavailable Vision response as a visual PASS and records that pixel verification was not completed.
- Added isolated policy tests for strong direct matches, narration-context matches, weak candidates, and single-keyword false matches.

## Pass 7 — adaptive search when a scene has too few candidates

The latest failed run (`122666073603`) showed that Scene 3 had only two metadata-qualified candidates after the initial six search variants. Both candidates were correctly rejected by pixel-level Vision checks. The run also encountered Wikimedia HTTP 429, Coverr HTTP 403, Groq HTTP 429, a DeepSeek insufficient-balance response, and a Gemini timeout. OpenRouter still returned a valid visual REJECT, so this was not a complete Vision outage and the Pass 6 metadata-only outage path correctly did not override that REJECT.

Pass 7 adds:
- A secondary search stage when the first pass finds fewer than five metadata-qualified candidates.
- Up to four broader, subject-preserving query variants. Weather/mood descriptors such as `mist` are removed from the search phrase when they make discovery too restrictive, while specific subjects such as jaguars, orchids, insects, and birds remain in their own independent alternatives.
- Alternative shot queries are broadened independently; they are never merged into one query that combines unrelated subjects.
- At least eight candidates are now eligible for pixel verification even though the checked-in legacy workflow still exports `FOOTAGE_VISION_CANDIDATE_LIMIT=5`. The existing workflow file remains unchanged by the ZIP installer.
- No relaxation of the visual verification threshold. A clip with an actual Vision REJECT remains rejected. Metadata-only acceptance is still limited to the separate, conservative policy for genuine Vision-service outages.

### Pass 7 validation
- 45/45 unit tests passed locally.
- Python compile check passed for the project files.
- The specific logged query `mist rising from tropical rainforest treetops` was checked against the new search-expansion function; it produces broader canopy/treetop queries without retaining `mist` as a hard search requirement.
- ZIP integrity and clean extraction were checked after packaging.

### Limits
The new search stage has not yet been exercised against the user's live GitHub Actions credentials or the external footage APIs. It improves discovery when the initial result pool is sparse; it cannot guarantee a usable clip when providers are rate-limited or no genuinely relevant footage exists. The visual verifier remains authoritative, so a scene can still stop with a diagnostic rather than use unrelated footage.


## Pass 8 — isolate text and vision provider health

The latest run showed that transient text-generation failures could place a matching model route on cooldown before footage verification began. Pass 8 scopes provider disable flags, route disable flags, and cooldown timers to the task (`text` or `vision`). Vision can now attempt its configured DeepSeek/Gemini routes even when those same model routes encountered errors during script generation. Visual acceptance thresholds and the conservative outage policy are unchanged. A regression test covers a text-route cooldown followed by successful use of the same model for vision.

See `PASS8_VALIDATION_REPORT.md` for the log-based diagnosis, validation results, and live-runtime caveat.


## Pass 9 — footage-first visual planning and reference gating

The latest Pass 8 run showed Scene 4 being planned as `tropical rainforest rainfall map` / `global precipitation map Amazon region`. The available stock clips were mostly live footage, so the Vision model correctly rejected them as not being rainfall maps; repeated rejected candidates then consumed the limited Vision-provider allowance.

Pass 9 changes:
- Keep footage as the default for natural processes that can be shown directly (rainfall, transpiration, evaporation, cooling, cloud formation, river flow, and forest ecology).
- A map/chart/diagram word in an AI visual query is no longer sufficient to force the institutional-reference path. The narration must show a concrete need for measured values, a geographic distribution, or internal scientific structure.
- Keep `visual_query` and its alternatives focused on real footage; reference-query wording is separate.
- Remove artifact-only words such as `map`, `chart`, `diagram`, `illustration`, and `infographic` from stock-footage query phrases.
- Add rainfall/rainforest-specific real-footage expansions, including rain falling in rainforest, raindrops on tropical leaves, and heavy rain over the canopy.
- Keep the original Vision confidence threshold and strict rejection behavior unchanged. An actual Vision REJECT is still final; the outage metadata fallback remains conservative and is explicitly labelled as not pixel-verified.

### Pass 9 validation
- `py_compile` passed for the changed production and test files.
- 49/49 unit tests passed, including new checks that generic rainfall-map requests downgrade to footage and that numerical geographic evidence still qualifies for a trusted reference.
- Query-builder checks confirm the failing map/chart queries are converted into real-footage searches.
- Live GitHub Actions, stock-provider responses, and external AI quotas have not been executed or verified in this local session. Code cannot replenish DeepSeek balance or override Groq/Gemini/OpenRouter rate limits; if all vision providers are down and no strong metadata candidate exists, the generator will still stop rather than use unrelated media.


## Pass 10 — evidence coverage and mandatory visual verification

Pass 10 supersedes the historical Pass 6 metadata-only Vision-outage fallback. The production path now requires pixel-level Vision verification for every selected scene, and a configured confidence value below 0.72 is ignored; operators may only make the threshold stricter.

Narrative quality and factuality changes:
- Every complete narration sentence must appear verbatim in the `fact_claims` evidence ledger with source URLs. The validator blocks missing sentence coverage before any voice-over is generated. A shared sentence splitter preserves decimal measurements and common abbreviations during fact checks and VO timing.
- Source pages must use HTTPS and an institutional/scientific allowlist. Redirects are followed manually, one hop at a time, only after the destination passes the same allowlist check.
- Fetched pages must contain a relevant passage for the claim. Page text comes from the actual fetched content; generated snippets and page titles are not accepted as evidence.
- Numeric/quantitative statements require two distinct source organisations; subdomains of one institution do not count as independent evidence.
- Claim, URL, redirect, and page-size limits bound requests and diagnostic artifacts. Failed source fetches and fact-provider outages produce a blocked fact-check report when possible.
- `production_report.json` embeds the complete fact-check evidence and verdict. The current GitHub Actions upload list does not separately upload `fact_check_report.json`, although the file is created and validated in the runner workspace.

Visual and operational changes:
- All downloaded footage and trusted reference visuals must pass the full scene narration + visual-focus pixel check. Metadata-only acceptance is removed from the production path.
- Exact duplicate video bytes are blocked between scenes; a second metadata collection call was removed.
- The original `.github/workflows/render.yml` is kept byte-for-byte unchanged because the self-upgrade workflow restores the checked-out version. Python code overrides the workflow's historical `FOOTAGE_VISION_REQUIRED=false` and candidate-limit-five values.

### Pass 10 local validation and limitations
- `python -m py_compile *.py tests/*.py`: passed.
- `python -m unittest discover -s tests -q`: **66/66 passed**.
- `healthcheck.py`: FFmpeg, ffprobe, libass, libx264, CPU PyTorch, requests, Pillow, SentencePiece and protobuf passed. The isolated build container lacks `edge_tts` and `transformers`; both are declared in `requirements.txt` and are installed by GitHub Actions.
- No live GitHub Actions run, actual provider call, source-page fetch for a real topic, or external stock-footage download was executed here. Production rendering still requires the configured provider secrets and real Actions environment.
- AI fact-checking cannot offer a mathematical guarantee. The evidence excerpts and verdicts must be reviewed before publishing, especially for high-stakes or time-sensitive statements.


## Pass 10.1 — structured-output and provider recovery patch

The failed run `122778749468` stopped during documentary planning. Groq returned JSON but the Scene 3 `visual_focus` did not overlap sufficiently with its primary query; DeepSeek returned `Insufficient Balance`; Gemini returned HTTP 503/high demand; and OpenRouter returned an unreadable response. No VO, footage search, or MP4 render began.

Pass 10.1 adds explicit JSON mode for Groq/DeepSeek/OpenRouter text and fact requests, one targeted same-provider repair after malformed JSON or validator rejection, more precise OpenRouter-compatible response parsing diagnostics, and a distinct HTTP 402 insufficient-balance condition. The prompt also asks for at least half the `visual_focus` tokens to appear verbatim in `visual_query`, matching the existing strict validator. Vision REJECT handling, the 0.72 visual-confidence floor, source evidence requirements, and fail-closed behavior are unchanged.

Validation: Python compilation passed and **71/71** unit tests passed, including a combined regression case that reproduces the provider failure order from the Actions log and recovers through OpenRouter after a targeted repair. No live API request or Actions render has been run here. DeepSeek account balance must be resolved by the account owner; external Gemini 503s cannot be prevented by project code. See `PASS10_1_ROUTING_VALIDATION.md`.


## Pass 10.3 — conservative scene-count recovery

The latest failure stopped before footage or rendering because Groq's plan returned a scene count other than eight. Pass 10.3 adds a bounded structural recovery step: it splits only a two-sentence scene whose two sentences both name the same visible focus, or merges adjacent one-sentence scenes only when their visible subjects and footage queries materially overlap. It preserves all narration and remaps every evidence-ledger scene index. Unsafe cases are not auto-rewritten; they continue through the strict validator, targeted provider repair, and multi-provider failover.

Validation: `python -m py_compile *.py tests/*.py` passed; `python -m unittest discover -s tests -q` passed **77/77 tests**. Live API and GitHub Actions rendering were not run; this patch cannot restore an empty DeepSeek balance or override external 503/429 limits.
