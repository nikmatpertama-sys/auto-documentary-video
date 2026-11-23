# PROJECT1 — Upgrade pass 2 (fallback correctness)

## Why this pass exists
The first upgrade had a scene-classification defect. Broad substring matching could interpret a word such as `planetary` as `planet`, and one topic-level cue could override the actual scene query. This caused unrelated fallback scenes to repeat the same orbital illustration.

## Changes in this pass
- Classify each fallback from its own visual query/title/narration before consulting the overall topic.
- Use word-boundary matching so `planetary` does not match `planet`.
- Add scene-specific rainforest illustrations: forest-floor orchids/ferns, jaguar and leafcutter ants, winding river/moisture flow, drought-stressed forest, and leaf/transpiration anatomy.
- Ensure each rainforest fallback uses the scene query to choose its illustration, rather than incidental details in a narration sentence.
- Preserve the existing fallback diagrams for hydrothermal vents, Earth layers, space, bacteria, volcanoes, animals, and climate.
- Include candidate counts, attempted checks, and provider/download failure details in `production_report.json` for future diagnosis.
- Do not silently claim that fallback scenes are verified stock footage.

## Tests
Run with:
`python -m unittest discover -s tests -v`

The tests cover scene-local classification, the `planetary` false-positive regression, distinct rainforest scene modes, and a rendered rainforest fallback MP4.

## Install
Upload this ZIP to the repository root on branch `project1` using the exact filename `auto-documentary-video-upgrade.zip`, then run the existing `Hybrid Documentary Generator` workflow on `project1`.
