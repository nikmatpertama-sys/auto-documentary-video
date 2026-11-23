# PROJECT1 — Upgrade pass 1

## Changes

- Replaced generic orbital fallback cards with semantic 2D explanatory illustrations for hydrothermal vents, Earth layers, space/orbits, bacteria/microbes, volcanoes, animals/anatomy, and climate/atmospheric heat flow.
- Unknown topics now use a labeled input → process → outcome diagram rather than decorative abstract geometry.
- Removed narration paragraphs from fallback artwork. The ASS subtitle renderer remains the single owner of spoken subtitles.
- Fallback motion render now uses restrained Ken Burns movement and x264 `medium` / CRF 19.
- Documentary prompt targets 160–185 words; AI-plan validation accepts 150–195 words. Actual TTS timeline is rejected unless it is 70–90 seconds.
- Local no-API script expanded to approximately 160+ words.
- Edge TTS keeps one selected voice and stable rate/pitch across sentences; it no longer silently switches voice midway through a video.
- Default final x264 preset changed from `ultrafast` to `medium`.
- Production report no longer treats an all-fallback video as fully visually verified; it reports local illustration count.
- The existing workflow self-upgrade mechanism is preserved. It intentionally restores its own workflow file before committing other extracted files; select `project1` in the GitHub branch picker before running it.
- Added semantic-category and FFmpeg fallback-render tests.

## Tests performed in this environment

- Python compile check: passed for all project Python modules.
- Unit tests: 10/10 passed (8 existing AI-router tests + 2 visual-fallback tests).
- Workflow YAML parse: passed. The existing workflow is preserved for compatibility with its self-upgrade step.
- Fallback MP4 render: passed; H.264, 1080×1920, 30 FPS, 2 seconds.
- Fallback image visually inspected.

## Not verified here

- Full `main.py` end-to-end generation was not run.
- Edge TTS and Transformers are absent from this audit environment; installing them was blocked because package-index DNS/network access was unavailable. The GitHub Actions workflow installs requirements before healthcheck.
- No live API, footage-provider, GitHub Secrets, or remote Actions run was available.
- No full 70–90 second final documentary MP4 was produced in this pass.
- The reference audio could not be audibly evaluated in this environment; no claim is made that Edge TTS now matches its exact acting style.

## Install

Replace the contents of the repository on branch `project1` with this ZIP. Do not merge or upload it to `main`. Keep existing GitHub Secrets; no API key values are included in this archive.
