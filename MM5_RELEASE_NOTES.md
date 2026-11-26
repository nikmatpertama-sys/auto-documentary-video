# MM5 release notes — based on MM4

MM5 is an additive upgrade from `mm4.zip`. It retains the MM4 Coverr integration and adds adaptive large-file downloads plus AI-verified dynamic 9:16 reframing. MM4 is not skipped or removed.

## Preserved from MM4

- Official Coverr API source, using the existing `COVERR_API_KEY` GitHub Actions secret.
- Coverr filters: `is_vertical:true`, `is_premium:false`, and `is_ai_generated:false`; catalog aspect ratios other than 9:16 are rejected when supplied.
- Coverr's per-source request pacing, per-run request ceiling, cache and persistent cooldown integration.
- Attribution to Coverr and the creator in `credits.txt` for the free Demo API tier.
- Existing Internet Archive, Wikimedia Commons, NASA and Google Search discovery paths.
- MM1 relevance ranking and rejection-guided search retry; MM2 FFprobe display-rotation handling; MM3 persistent rate-limit state, independent source caches, request governor and NASA manifest cache.
- Pixabay and Pexels remain disabled by default.

## Adaptive footage download

- Ordinary candidate budget: 180 MB.
- High-relevance candidate budget (metadata score >= 0.72): 650 MB.
- Exceptional-relevance candidate budget (metadata score >= 0.86): 900 MB.
- Known file sizes are checked before transfer where source metadata exposes them.
- Files are streamed in 1 MiB chunks. An interrupted transfer gets a bounded HTTP Range resume attempt when supported; servers that ignore Range safely restart the partial transfer.
- Download time budget is 15 minutes and the process preserves at least 1.5 GB free disk space.
- Adaptive budgets do not override relevance, native aspect, crop-fit, visual verification or license requirements.

## AI content-aware 9:16 reframing

- Native 9:16 remains preferred. Coverr stays a native-vertical source; its own API filters and downloaded-file FFprobe check remain unchanged.
- Other sources may provide eligible landscape footage only when metadata relevance is sufficient, the downloaded display aspect is 1.18–2.40 and the short side is at least 640 px.
- Gemini, Groq or OpenRouter vision examines six timestamped frames and returns normalized bounding boxes for the main subject and all scene-critical content.
- Local validation requires every box to fit inside the 9:16 crop window with a safety margin and confidence >= 0.55. Too-wide subjects, missing/invalid boxes, edge clipping, excessive pan speed or uncertain composition reject the candidate.
- The crop path is smoothed and interpolated over time, using FFmpeg to follow the subject. It safely repeats the crop path when a short clip loops.
- Three frames from the proposed crop are independently checked by the configured visual verifier. A landscape source is selected only after this post-crop verification passes.
- When multi-frame crop vision is unavailable, landscape candidates are rejected; no metadata-only or center-crop bypass is used.

## Runtime and workflow

- No new secrets are introduced. The existing workflow already maps `COVERR_API_KEY` and the supported visual-provider credentials.
- The ZIP's bundled workflow performs complete unittest discovery. The repository's permanent `render.yml` is not overwritten by the ZIP auto-extractor.
- No direct GitHub branch or repository changes are part of this upgrade.

## Validation

- Python syntax compilation passed.
- 87 unit/integration tests passed, including the MM4 Coverr tests and MM5 adaptive-download/crop tests.
- A synthetic landscape test clip was rendered through FFmpeg to 1080x1920 and inspected using FFprobe.
- Coverr requests and AI-provider outputs were mocked in tests; live API authentication, live stock-footage availability and a production end-to-end run were not tested.
- The local environment lacked the optional `edge-tts` package and had no package-network access. The full unit suite was therefore run with a minimal import-only `edge_tts` stub; no TTS/voice generation path was exercised by that stub.
