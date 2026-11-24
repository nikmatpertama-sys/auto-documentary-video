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

Upload this ZIP to the repository root on branch `project1` using the exact filename `auto-documentary-video-upgrade.zip`, commit it, then run **Hybrid Documentary Generator** from branch `project1`.


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
