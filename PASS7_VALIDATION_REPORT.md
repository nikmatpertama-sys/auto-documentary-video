# Pass 7 validation report

## Root cause from run 122666073603

The script, final voice-over, VO timing and subtitles all completed. The render stopped at Scene 3 in `main.py::fetch_footage`.

- Initial search produced only 2 metadata-qualified candidates across 6 query variants.
- Both candidates were downloaded and rejected by actual frame inspection: one showed a stream/waterfall close-up, the other showed fern fronds rather than mist over treetops.
- Groq later hit HTTP 429, DeepSeek returned insufficient balance, and Gemini timed out. OpenRouter still returned a valid REJECT, so this was not a total Vision outage; Pass 6 correctly did not convert that verdict into metadata-only acceptance.
- Wikimedia Commons was rate-limited (HTTP 429), Coverr returned HTTP 403, and Pexels was disabled because no `PEXELS_API_KEY` was configured.
- The checked-in workflow also exported `FOOTAGE_VISION_CANDIDATE_LIMIT=5`, overriding the Pass 6 code default of 8.

## Pass 7 changes

1. Search expands only when the initial metadata-qualified candidate pool contains fewer than five items.
2. Up to four extra queries broaden the requested subject without keeping transient weather/mood terms as mandatory search terms.
3. Separate alternative-shot queries are expanded independently. Specific organisms remain part of their own search variants.
4. The code enforces a minimum of 8 candidates for pixel verification despite the legacy workflow value of 5.
5. Pixel-level Vision verification and its confidence threshold are unchanged. A valid REJECT is never overridden by search metadata.

## Local validation

- 45/45 unit tests passed.
- `compileall` passed.
- The standalone `healthcheck.py` was attempted but did not pass in this isolated local environment because `edge_tts` and `transformers` are not installed here; the GitHub workflow installs the declared requirements before its own health check.
- The logged Scene 3 query produced subject-preserving fallback search variants locally.
- ZIP integrity and clean extraction passed.

## Not verified

The new search behavior has not been run with the user's live GitHub Actions secrets or live Pixabay/NASA/Commons/Coverr API conditions. This patch reduces premature failure from sparse first-pass search results; it cannot guarantee footage availability during external API outages or where no genuinely relevant clip exists.
