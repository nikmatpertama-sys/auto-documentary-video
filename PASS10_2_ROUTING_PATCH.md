# Pass 10.2 — OpenRouter Output-Budget Recovery

## Evidence from the failed Pass 10.1 Actions run

The run completed setup, dependency installation, the health check, and all 71 then-current unit tests. It stopped during step `1/7` (documentary plan generation), before factual source checks, TTS, footage search, visual verification, or FFmpeg rendering.

The live provider outcomes were:

- Groq: HTTP 429 quota/rate limit.
- DeepSeek: HTTP 402 insufficient balance/payment.
- Gemini: read timeout (the previous run had shown HTTP 503 high demand).
- OpenRouter `openrouter/free`: after one repair attempt, the response still had empty assistant `content`, `finish_reason=length`, and reasoning-related message fields.

OpenRouter documents that reasoning tokens count against the same `max_tokens` completion budget. A reasoning model can consume the budget and return `finish_reason: "length"` with empty visible content. This matches the response shape in the Actions log.

## Changes

1. Retain the existing dynamic `openrouter/free` route and append a second text/fact route through the same existing OpenRouter key: `baidu/ernie-5.5-flash-preview:free`.
2. The ERNIE fallback uses JSON mode and low reasoning effort. OpenRouter's current ERNIE model page documents JSON structured-output support and the `reasoning` parameter.
3. If the dynamic route returns empty/truncated content twice, the router now continues to ERNIE instead of exhausting the entire provider list.
4. Preserve errors from the first transient-failure pass when retrying a route, so final diagnostics do not hide the earlier Groq/DeepSeek/OpenRouter outcomes behind only the last Gemini timeout.
5. No visual-verification threshold, factual-source requirement, scene validator, or fail-closed policy was relaxed.

## Validation performed

- `python -m py_compile *.py tests/*.py`: passed.
- `python -m unittest discover -s tests -v`: **73/73 passed**.
- Added regression tests for reasoning-only OpenRouter output falling through to ERNIE and for preserving the first failure across the bounded transient retry.
- Local `healthcheck.py` confirmed requests, CPU PyTorch, SentencePiece, protobuf, Pillow, safetensors, FFmpeg, subtitles, and libx264. The local sandbox does not have `edge_tts` or `transformers`; both remain in `requirements.txt` and are installed by the GitHub Actions dependency-install step. No claim is made that these two local checks passed.
- No live API call was made with the user's secrets from this environment. This patch therefore does not claim that a real documentary render has succeeded.

## Notes

- DeepSeek's HTTP 402 still requires account balance/payment correction if that provider is to be used.
- Groq's HTTP 429 still means the account/model quota or rate limit was reached; a code patch cannot top up or reset provider quota.
- OpenRouter's free model variants are rate limited. The fallback uses the same OpenRouter API key and does not require a new secret.
- If all routes remain unavailable or return invalid data, the generator still stops safely rather than producing an unaudited script.
