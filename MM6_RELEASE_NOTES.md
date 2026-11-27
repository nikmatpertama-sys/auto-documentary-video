# MM6 release notes — reference-style vertical documentary production line

MM6 keeps the MM5 footage engine (Coverr, Internet Archive, Wikimedia Commons,
NASA, Google discovery, adaptive downloads, AI-verified 9:16 reframing) and adds
the production stages the reference format needs. All additions run on free
software: Edge TTS (no key), Wikipedia/Wikimedia API (no key), FFmpeg synthesis,
and the free LLM routes the pipeline already used.

## Added stages

1. **Research** (`research.py`) — queries the Wikipedia API (id + en) with no key,
   stores up to four source articles and a bounded evidence brief in
   `research.json`, including URL, licence and retrieval time.
2. **Grounded scriptwriting** — `SCRIPTWRITER_MASTER_PROMPT.md` now receives
   `[[LANGUAGE]]`, `[[LANGUAGE_NATIVE]]` and `[[RESEARCH_BRIEF]]`. The writer must
   base specific claims on the supplied material and may not invent numbers.
3. **Fact-checking** (`fact_check_plan`) — extracts numeric/absolute claims,
   asks a free provider for a per-claim verdict with evidence, repairs the plan
   once when a claim is contradicted or unsupported, re-verifies, and aborts the
   run if a contradiction survives. Results are stored in `fact_check.json`.
4. **Reference presentation** (`reference_style.py`) — Indonesian-first language
   profile, bold centred uppercase subtitles sized for 1080x1920 with a
   `MarginV` of 420 px so Facebook Reels UI never covers them, and the opening
   hook rendered as an ASS event (no `drawtext` dependency).
5. **Audio mix** — a locally synthesised royalty-free ambient music bed plus
   procedural transition whooshes and a hook impact, side-chain ducked under the
   narration, then `loudnorm` followed by a limiter. No third-party audio is
   downloaded, so there is no music licence risk.
6. **Duration recovery** — the 120-word floor does not guarantee 70 seconds. The
   pipeline measures the real voice-over, deepens the script (one provider pass),
   and, as a last resort, uses a slower but still natural delivery. Silence
   padding was removed entirely.
7. **Quality control** (`qc.py`) — after rendering it verifies 1080x1920, the
   70-second floor, that the file ends with the voice-over, audio presence,
   clipping, black gaps, caption events, word count, scene count and footage
   reuse, writing `qc_report.json` and failing the run on a critical problem.
8. **Asset documentation** — `copyright_report.json` records every clip's
   source, creator, URL, licence, voice provider, music/SFX provenance and font.

## Format guarantees

- 1080x1920, 30 fps, H.264 + AAC, `+faststart`.
- 120-200 words enforced before rendering; 8 scenes mapped to exact sentences.
- Minimum 70 seconds, no upper bound: the video ends when the narration ends
  (0.30 s tail so the last phoneme is not clipped).
- Voice-over is the primary audio element; music sits 23 dB under it before
  ducking and SFX 17 dB under.
- Subtitles and the hook use the bundled DejaVu fonts (`fonts/`) so typography
  is identical on a runner without system fonts.

## Zero-cost policy

No paid API is required for any stage. Edge TTS is the guaranteed narration path
(Speechify is used only when an existing free account key is configured).
Research, music, sound design, captions and QC use only local tools or
key-less public APIs.

## Validation performed (this release)

- `python -m unittest discover` — 110 tests pass (87 existing + 23 new MM6).
- Live research check: `research_topic("Komodo")` returned 4 articles and a
  5.8 kB evidence brief from the public Wikipedia API.
- Fact-check/repair loop exercised with a mocked provider: contradicted claims
  triggered one repair pass and a re-verification.
- Local end-to-end harness (`tools/local_e2e.py`): 190-word Indonesian script →
  Edge TTS (`id-ID-ArdiNeural`) 83.59 s → captions → 8 edited scenes → music +
  SFX mix → 1080x1920 H.264 render → QC pass (duration 83.567 s, mean -17.5 dBFS,
  peak -1.1 dBFS, 62 caption events, no black gaps).
- Rendered frames inspected visually: hook title and uppercase subtitles render
  in the intended safe area.

## Known limitations

- No AI vision keys were available in this environment, so the live footage
  search/vision-verification path was not exercised end-to-end; the harness used
  synthetic vertical clips. The footage engine itself is unchanged from MM5.
- The script, research and fact-check stages require at least one free LLM
  credential (Groq/Gemini/OpenRouter/Cloudflare) as before; without one the run
  stops with a clear configuration error.
- No system can guarantee zero Facebook claims. Licences are recorded and
  constrained (public-domain/CC/Coverr, plus originally synthesised audio), and
  the publisher must still review `copyright_report.json`.
