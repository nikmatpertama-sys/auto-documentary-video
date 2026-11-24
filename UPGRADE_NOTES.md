# PROJECT1 — Presentation and source-integrity upgrade (Pass 4)

## Problems observed in `revisi8`

1. The opening title/hook was one long `drawtext` line. The render cropped text at the right edge and the sanitizing regex deleted Unicode punctuation such as the curly apostrophe in `Amazon’s`.
2. Captions were assembled into fixed word-count chunks. This ignored clause boundaries and removed punctuation, so phrases could break unnaturally even though TTS word-boundary events existed.
3. When visual verification providers were unavailable, some scenes fell back to locally fabricated explanatory illustrations. Those illustrations were not authoritative evidence and could be semantically wrong.

## Changes in Pass 4

### Opening title/hook
- Replace one-line FFmpeg `drawtext` with a dedicated transparent Pillow overlay.
- Measure text using actual font pixels and wrap both title and hook within the safe width.
- Preserve Unicode punctuation and complete title/hook text.
- Grow the card from measured line counts; never silently truncate overflow. Stop with an explicit error if unusually long text cannot fit the safe opening area.
- Force scene 1 to use footage so the opening title/hook overlay is not skipped by a reference-image scene.
- Save the computed layout in `opening_layout.json` and include it in `production_report.json`.

### Subtitle pacing
- Align Edge TTS word-boundary events to the original script tokens to restore commas, apostrophes and sentence-ending punctuation.
- Reject incomplete token alignment rather than silently dropping words.
- Group captions at sentence/clause punctuation, natural timing gaps, maximum phrase length, and phrase duration; no fixed five-word rule.
- Add ASS word-level karaoke timing (`\\kf`) using TTS offsets/durations so the highlighted word follows the spoken word.
- Prevent phrases from crossing sentence boundaries and cap events at the measured voice-over end.
- If a TTS word cannot be matched to a reliable word boundary, fail the caption stage instead of producing misleading synchronization.

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

Tests cover title wrapping and Unicode punctuation, exact caption-token alignment, natural phrase groups, karaoke tags, map/diagram policy, source provenance, reusable licenses, and rejection of NASA assets explicitly marked as third-party/restricted. The full GitHub Actions run and external API availability still need validation in the user's repository.

## Install

Upload this ZIP to the repository root on branch `project1` using the exact filename `auto-documentary-video-upgrade.zip`, commit it, then run **Hybrid Documentary Generator** from branch `project1`.
