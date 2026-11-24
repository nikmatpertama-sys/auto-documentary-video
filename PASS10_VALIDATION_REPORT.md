# Pass 10 — Evidence and Visual Quality Gates

## Scope

Pass 10 addresses the observed Pass 9 problems without changing the 1080×1920, 30 FPS, 70–90 second target or replacing the existing footage providers and AI stack.

## Root causes confirmed in Pass 9 source

1. `.github/workflows/render.yml` set `FOOTAGE_VISION_REQUIRED: "false"`.
2. `main.py` defaulted `VISION_REQUIRED` to false and contained a metadata-only acceptance path when Vision providers were unavailable.
3. `verify_footage_visually` primarily checked the scene visual query and explicitly did not require the clip to support the complete narration. This allowed an asset to show one isolated keyword while other concrete subjects or the central idea of the scene were unsupported.
4. The script planner asked the model not to invent statistics or URLs, but there was no source-fetching, citation, or claim-entailment gate before TTS.
5. The same search queries were collected twice per scene, causing unnecessary duplicate footage API calls.
6. The production report could legitimately contain `visual_verification_required: false` and `all_scenes_visually_verified: null`, so it did not establish that every scene had passed a mandatory pixel check.

## Pass 10 changes

### Narrative factuality

- Added `fact_check.py` with an HTTPS trusted-source allowlist, redirect validation, bounded source-page fetching, HTML visible-text extraction, and claim-specific evidence excerpts.
- The planner must return an exact-quote evidence ledger. Every narration sentence—not just a chosen subset—must map to one complete entry in its scene, and every entry must have source URLs.
- A separate fact-review route (`AI_FACT_PROVIDER_ORDER`) checks fetched page excerpts against the complete script and reports unsupported, contradicted, or uncited facts.
- Quantitative claims containing digits/measurements/percentages require at least two distinct trusted source organizations (subdomains of the same institution do not count), both of which must be selected by the fact auditor as supporting evidence.
- Every selected URL is fetched from an allowlisted HTTPS host. Redirects are checked one hop at a time and untrusted destinations are rejected before the request is sent. Each source excerpt must contain a minimum lexical match to the claim; page titles or unrelated page text are not sufficient.
- A shared sentence splitter keeps decimal measurements (for example, 20.5) intact when aligning claim sentences and voice-over timing, and protects common abbreviations from accidental sentence breaks.
- Claim/source counts and page sizes are bounded. Numeric claims and full narration sentence coverage also have deterministic backstops.
- Unsupported facts, invalid or unreachable URLs, insufficient page content, or failed fact-provider calls stop the run before voice-over generation.
- Removed the generic unaudited local-template fallback when script-generation providers fail.
- `fact_check_report.json` records source pages, excerpts, verdicts and provider information. The complete report is also embedded in `production_report.json`.

### Visual matching and quality

- Pixel-level Vision verification is mandatory in production. The confidence threshold has a hard floor of 0.72; environment settings may make it stricter but cannot lower it.
- Metadata-only footage/reference acceptance is disabled. A valid REJECT is kept as a rejection; Vision outages cause candidate rejection, not fallback acceptance.
- The planner must define one concrete `visual_focus` per scene, align it with the primary query, keep the narration to one or two sentences, and avoid bundling unrelated subjects in one scene.
- The frame verifier checks both the required visible focus and whether the clip supports the complete scene idea. Generic footage is explicitly rejected for specific species/objects/processes that are not visible.
- Published reference visuals must also pass pixel-level verification.
- Exact duplicate video files across source providers are blocked using SHA-256 hashes.
- Removed a duplicate call to the full search-collection pass per scene.
- Candidate query identity remains attached to each candidate for thumbnail reranking.

### Reporting

- Each scene record now includes `visual_focus`, actual Vision method, confidence and reason.
- `visual_verification_required` is always true; `all_scenes_visually_verified` is a real boolean, not `null` because the policy was switched off.
- The production report embeds the complete factual validation report and explicit no-metadata-fallback policy.

## Local validation

- Python syntax compilation: passed for all project and test modules, including the shared decimal-aware sentence splitter.
- Unit tests: 66/66 passed, including source allowlist/fetch/parser/redirect tests, full narration sentence coverage, quantitative dual-source checks, independent fact-provider routing, and the non-lowerable 0.72 Vision floor.
- `healthcheck.py` verified Python, requests, CPU PyTorch, Pillow, FFmpeg, ffprobe, libass and libx264 in this build container. It did not pass completely because `edge_tts` and `transformers` are not installed in this isolated container; both are declared in `requirements.txt` and are installed by GitHub Actions before runtime.
- No live GitHub Actions render, live AI API request, actual source-page fetch for a real generated script, or external stock-footage request was run from this environment.
- The original `.github/workflows/render.yml` was kept byte-for-byte unchanged because the existing self-upgrade workflow restores that file from the checked-out branch. The new production gates are enforced in Python and override its legacy `FOOTAGE_VISION_REQUIRED=false` and candidate-limit settings.

## Expected production behaviour

A run can now stop before voice-over if even one narration sentence lacks source coverage, a source is inaccessible/irrelevant, or the fact auditor cannot support every claim. A run can stop during footage selection if Vision is unavailable or no candidate reaches the hard 0.72 confidence floor. This is intentional: the pipeline no longer trades factual or visual quality for a completed MP4.

## Limitations

AI-assisted source entailment is stronger than a prompt-only instruction but cannot mathematically guarantee truth. Humans should review the source excerpts and verdicts embedded in `production_report.json`, especially for legal, medical, financial, safety-critical or rapidly changing claims. The current workflow's artifact file list uploads `production_report.json` (which embeds the full report); `fact_check_report.json` is also written and validated in the runner workspace but is not a separate artifact unless the workflow's upload list is later expanded. Only allowlisted sources readable as HTML/plain text are accepted; inaccessible or PDF-only evidence blocks the run rather than being guessed.
