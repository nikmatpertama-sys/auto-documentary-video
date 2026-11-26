# Auto Documentary Video — Version 10.12

Automated vertical documentary generator. It builds a scene plan, searches for relevant real-world footage, performs visual verification, generates narration and captions, and renders the final video with FFmpeg.

## Master scriptwriter prompt

`SCRIPTWRITER_MASTER_PROMPT.md` remains the single source of truth for the generator's professional documentary scriptwriter role. `main.py` loads it at runtime through `documentary_prompt()`. The prompt enforces factual discipline, an eight-scene structure, a 145–165-word narration target, and practical footage queries. Its claim-by-claim audit is part of the same prompt; it does not create a second independent fact-check API call.

## Footage source selection — version 10.12

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

Add at least one AI provider key: `GEMINI_API_KEY`, `GROQ_API_KEY`, or `OPENROUTER_API_KEY`. `PIXABAY_API_KEY` and `PEXELS_API_KEY` are optional fallback-provider keys. Internet Archive, Wikimedia Commons, and NASA searches do not require API keys.

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
