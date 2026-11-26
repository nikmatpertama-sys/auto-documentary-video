# Upgrade 10.13 — footage integrity and readable text

## Problems addressed

The inspected 10.12 render had a clipped opening hook, captions that crossed sentence boundaries, and shots that were only loosely related to the scene narration. The final file was encoded as 1080×1920, but its renderer used a center-crop filter and did not require the downloaded source to be native 9:16.

## Changes

### 1. Title / hook layout
- Wraps the complete hook into centered lines with a dark backing plate and safe top placement.
- Calculates font size from the longest rendered line.
- Removes the previous arbitrary 90-character truncation.

### 2. Subtitle consistency
- Generates caption groups independently per spoken sentence.
- Prevents the last words of one sentence from being grouped with the first words of the next sentence.
- Restores sentence-ending punctuation when the TTS word-boundary data leaves it out.
- Uses a consistent typeface, size, outline, bottom-safe margin, and maximum word/character count.

### 3. Footage relevance
- Applies the primary scene query as a mandatory relevance constraint even when fallback search wording is broader.
- Keeps concrete subjects and shot types mandatory (for example, jaguar, overhead canopy, and rain on glass).
- Rejects known context conflicts such as volcanic ash when the scene asks for moisture transport.
- Tightens the visual reviewer instructions so a generic forest or atmosphere shot cannot pass as a specific requested scene.

### 4. Native 9:16 source requirement
- Probes each downloaded clip with `ffprobe`, including display rotation metadata.
- Rejects non-9:16 source files before vision verification, even when a provider lists them as vertical.
- Prefers true 9:16 renditions where a provider offers multiple file sizes.
- Rechecks aspect ratio at render time and uses fit-plus-pad rather than scale-up-and-crop.

## Validation

- `main.py` passes Python syntax compilation.
- Synthetic 720×1280 native portrait accepted.
- Synthetic 1280×720 landscape rejected.
- Synthetic 1080×1350 (4:5) vertical rejected.
- Scene-anchor checks reject bird-nest footage for an aerial rainforest-canopy request, volcanic-ash metadata for a moisture-plume request, butterflies for a jaguar request, and generic forest data imagery for a raindrop-on-window request.
- Caption grouping test confirms that sentence boundaries are retained.
- Local FFmpeg render test outputs 1080×1920 from an approved native 9:16 source without using a crop filter.

A full network-backed production run has not been performed in this offline patch session. The existing master scriptwriter prompt and provider list are unchanged.
