# Auto Documentary Video Generator — PROJECT1

Automated vertical mini-documentary generator using AI-assisted story planning, licensed stock footage and published institutional visuals, Edge TTS narration, word-synchronized captions, and FFmpeg rendering.

## Output

- `documentary.mp4` — 1080 × 1920, 30 FPS, H.264/AAC.
- `plan.json` — script, scene plan, selected media and provenance metadata.
- `credits.txt` — asset creator, source page, license/usage information, and visual-verification status.
- `production_report.json` — per-scene source, verification method, reference visual details, opening layout, and caption-group count.
- `timing.json` — sentence events, word-level timings, and caption phrase groups.
- `output/voiceover.mp3` and `output/captions.ass` — final voice-over and word-synchronized subtitle file.

## GitHub Actions setup

Switch the repository branch selector to `project1` before uploading the upgrade ZIP or running **Actions → Hybrid Documentary Generator → Run workflow**. The self-upgrade workflow commits to the currently selected branch. Do not run it from `main`.

Upload the file `auto-documentary-video-upgrade.zip` to the repository root, commit it, and run the workflow with branch `project1`. Enter the documentary topic in the workflow input.

Optional GitHub Actions secrets:

- `GROQ_API_KEY`
- `DEEPSEEK_API_KEY`
- `GEMINI_API_KEY`
- `OPENROUTER_API_KEY`
- `PIXABAY_API_KEY`
- `PEXELS_API_KEY`
- `COVERR_API_KEY`

Text and visual AI providers depend on valid keys, model availability, service status, and quota. Stock-footage providers are independently disabled for the remainder of a run after authentication, permission, or rate-limit errors (HTTP 401, 403, or 429).

## Visual sourcing policy

The production path does **not** create procedural scientific diagrams, charts, tables, maps, anatomy plates, or data graphics. When a scene needs this kind of evidence visual, it searches for an already-published visual from an institutional source. It currently searches NASA's image library and Wikimedia Commons records that include a linked primary institutional source and an accepted reusable license.

- NASA assets explicitly marked as third-party copyrighted/restricted are skipped. NASA media usage rules still apply; the source is credited and no endorsement is implied.
- Commons images are accepted only when a linked primary source belongs to a recognised institutional domain and the file has a reusable license such as CC0, public domain, CC BY, or CC BY-SA. Attribution and license details are included in `credits.txt`.
- The published image's scientific content is preserved. It is fitted into the vertical canvas without cropping its data or labels; the frame adds only the title and source credit.
- If no suitable published visual is found, the generator tries relevant real footage. It does not invent a replacement diagram. If no acceptable footage or published visual exists, the run stops with a diagnostic rather than silently presenting an unrelated illustration.
- The legacy `visual_fallback.py` module remains in the repository for compatibility and its unit tests, but the production `main.py` path does not call it.

The local SigLIP model may rank stock-footage thumbnails. It is not equivalent to checking every frame. AI visual verification is attempted when configured; if all vision providers are unavailable, the production report clearly distinguishes metadata-only acceptance from pixel-verified footage.

## Opening title and hook

The opening title/hook is rendered as a separate transparent Pillow overlay. Text is wrapped using actual pixel widths, preserves Unicode punctuation (including curly apostrophes), and is checked before rendering. The first scene is forced to use moving footage so the opening card is always available. The build fails clearly if supplied title/hook text cannot fit safely; it does not silently clip words at the screen edge.

## Narration and subtitles

- Narration uses Edge TTS word-boundary events.
- Word timings are aligned back to the original script so punctuation is retained.
- Captions are grouped by punctuation, clause boundaries, timing gaps, and phrase length—not fixed five-word chunks.
- Each phrase uses word-level ASS karaoke timing from the TTS boundaries, with the currently spoken word highlighted.
- If a word cannot be aligned reliably to a TTS boundary, caption generation stops instead of silently omitting or drifting that word.
- If actual voice-over duration is slightly outside 70–90 seconds, a small pitch-preserving FFmpeg `atempo` adjustment may normalize it, and all word/sentence timestamps are scaled by the measured duration change. Larger deviations are rejected rather than forcing unnatural speech.

## Local setup

Requirements: Python 3.11+, FFmpeg/ffprobe with libass subtitles, and packages in `requirements.txt`.

```bash
python -m pip install -r requirements.txt
python main.py
```

Set provider keys as environment variables before running locally.

## Important notes

- Verify the generated MP4 and `credits.txt` before publishing.
- Check each source page and any applicable license terms, especially for monetized/commercial publishing.
- AI-generated scripts still need editorial fact-checking.
- Provider availability, quotas, and topic-specific footage coverage can affect whether all eight scenes can be completed.
