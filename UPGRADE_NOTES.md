# PROJECT1 — Upgrade pass 3 (run failure and source handling)

## Why this pass exists
The first upgrade had a scene-classification defect. Broad substring matching could interpret a word such as `planetary` as `planet`, and one topic-level cue could override the actual scene query. This caused unrelated fallback scenes to repeat the same orbital illustration.

## Retained fallback corrections from pass 2
- Classify each fallback from its own visual query/title/narration before consulting the overall topic.
- Use word-boundary matching so `planetary` does not match `planet`.
- Add scene-specific rainforest illustrations: forest-floor orchids/ferns, jaguar and leafcutter ants, winding river/moisture flow, drought-stressed forest, and leaf/transpiration anatomy.
- Ensure each rainforest fallback uses the scene query to choose its illustration, rather than incidental details in a narration sentence.
- Preserve the existing fallback diagrams for hydrothermal vents, Earth layers, space, bacteria, volcanoes, animals, and climate.
- Include candidate counts, attempted checks, and provider/download failure details in `production_report.json` for future diagnosis.
- Do not silently claim that fallback scenes are verified stock footage.


## Changes in this pass
- Fix the run-stopping case where measured Edge TTS narration was just below 70 seconds (the supplied log measured 66.92 seconds).
- Measure the final concatenated WAV with ffprobe and use a small, pitch-preserving FFmpeg `atempo` adjustment toward 71 or 89 seconds only when necessary.
- Scale every word boundary and sentence boundary by the measured audio change before writing ASS captions and `timing.json`, keeping subtitles and scene cuts synchronized with the adjusted voice-over.
- Limit normalization to a safe `atempo` range of 0.85-1.15; reject larger mismatches rather than making the narrator sound unnaturally slow or fast.
- Generate and validate the voice-over before starting footage-provider searches, avoiding expensive API calls when narration cannot be used.
- Disable a footage provider for the rest of the run after HTTP 401, 403, or 429, avoiding repeated requests to Coverr after the 403 responses seen in the supplied log.
- Add unit tests for short/long narration timing, invalid durations, and footage-provider HTTP failure handling.

## Tests
Run with:
`python -m unittest discover -s tests -v`

The tests cover scene-local classification, the `planetary` false-positive regression, distinct rainforest scene modes, a rendered rainforest fallback MP4, and narration duration normalization decisions.

## Install
Upload this ZIP to the repository root on branch `project1` using the exact filename `auto-documentary-video-upgrade.zip`, then run the existing `Hybrid Documentary Generator` workflow on `project1`.
