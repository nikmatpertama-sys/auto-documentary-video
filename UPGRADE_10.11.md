# Project 1 — Version 10.11 upgrade notes

## Fixes based on the failed 10.10 run

- Adds targeted fallback searches for documentary fieldwork, scientists, forest researchers, humidity measurements, weather stations, sensors, and meteorological instruments. The fallback generator now considers the scene narration as well as the visual-query strings.
- Keeps distinct shot alternatives separate: researchers in the field, scientists collecting samples, and close-ups of environmental instruments can each be searched as their own visual idea. Every selected candidate must still pass the existing visual verification.
- Adds mandatory metadata anchors for research activity and field instruments when those concepts are explicitly requested. This prevents loose word overlap from being enough to admit a candidate.
- Excludes obvious spaceflight/Shuttle footage from non-space scenes before spending a visual-verification call.
- Extends sparse-scene recovery to fill the configured metadata candidate pool rather than stopping once 20 candidates have been found.

## Wikimedia Commons reliability

- Adds a polite request interval and bounded HTTP 429 retries that honor Retry-After when available.
- If a source remains rate-limited, it enters a timed cooldown and can recover later in the same workflow run. It is no longer disabled permanently for the full run after one 429.
- Uses a new source-cache version and avoids caching partial cross-provider searches, preventing a transient 429 from poisoning the six-hour metadata cache.
- Gives rate-limited visual-verification providers a minimum 30-second in-run cooldown (or a longer provider-supplied delay) to reduce repeated 429 calls and unnecessary retries.

## Failure diagnostics

- Writes `failure_report.json` and `stage_report.json` when the pipeline fails.
- Writes `output/pipeline.log` with a tee of console output, so the existing workflow's failure-artifact upload step has real files to collect.
- Moves missing-key/topic validation inside the diagnostic wrapper so these startup errors are reported too.

## Wikipedia decision

- Wikipedia is not added as a separate footage provider. Video uploads and media-file search already belong to Wikimedia Commons, which is already in the source list. A separate Wikipedia endpoint would mainly return encyclopedia pages and duplicate Commons-linked media rather than provide an independent footage catalogue.

## Preserved from 10.10

- Strict source selection priority: Internet Archive → Wikimedia Commons → NASA → Pexels → Pixabay.
- Relevance ranking remains within each source tier; lower-tier sources cannot outrank higher-tier sources.
- Source license checks, direct download checks, visual verification, and related-scene reuse remain enabled.
- The `SCRIPTWRITER_MASTER_PROMPT.md` file is preserved byte-for-byte, and its runtime loader remains active in `main.py`.
- The outdated `.github/workflows/render.yml` is not included. The ZIP must not overwrite the permanent-apply workflow in the user's repository.

## Validation performed

- Python syntax compilation.
- ZIP integrity and CRC checks.
- Unit-style checks for scene 7 fallback generation, strict anchor rejection of Shuttle footage, acceptance of weather-station/instrument metadata, source-tier ordering, and prompt integrity.
- Diagnostic file creation checked with a simulated pipeline exception.

A full networked render with the user's live API credentials is not claimed by this package build.
