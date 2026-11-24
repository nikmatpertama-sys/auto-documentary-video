# Pass 10.1 — Provider Routing Recovery Patch

## Failure evidence from GitHub Actions run 122778749468

The run failed inside `gemini_plan()` before fact-checking, text-to-speech, subtitle generation, footage search, or FFmpeg rendering.

Observed provider outcomes in `12_Generate documentary`:

1. Groq (`openai/gpt-oss-120b`) returned JSON that failed the project validator: `Scene 3 visual_focus is not represented by its primary visual_query.`
2. DeepSeek (`deepseek-flash`) returned `Insufficient Balance`.
3. Gemini (`gemini-3.8-flash`) returned HTTP 503 and reported high demand.
4. OpenRouter (`openrouter/free`) returned an unreadable OpenAI-compatible response.
5. The router exhausted its routes and raised `ProviderRoutingError`; the job exited with code 1.

The configured model identifiers were checked against current provider model documentation. This patch does not replace model IDs merely because of the transient failures in that log.

## Changes in Pass 10.1

### Structured-output requests

- Groq, DeepSeek, and OpenRouter text/fact calls now explicitly send `response_format: {"type":"json_object"}`.
- This is especially relevant to OpenRouter's `openrouter/free` dynamic router: asking for JSON mode lets the router filter for models that advertise structured-output support.
- Gemini already requests `application/json` through its Generate Content configuration; that path was retained.

### Bounded same-provider repair

- A text/fact response that is malformed JSON or fails the application's schema/semantic validator gets exactly one same-provider repair attempt.
- The retry includes the actual validation error and requests a complete corrected JSON object.
- If the repair also fails, the route is disabled for the current task and normal provider failover continues.
- Vision decisions are unchanged: a valid visual REJECT remains final and is never retried as though it were an outage.
- The 0.72 minimum visual confidence, factual-source gate, and fail-closed production policy are unchanged.

### OpenAI-compatible response parsing

- Parses string and text-block list content, as well as a `parsed` JSON object when supplied.
- Distinguishes invalid/non-JSON response bodies, missing chat-completion choices, missing assistant messages, and empty content in diagnostics.
- Does not print raw response bodies or credentials into logs.

### Billing diagnosis

- HTTP 402 is now logged as an insufficient-balance/payment condition and disables that provider for the current task in that run.
- It is no longer mislabeled as a generic temporary service outage.
- A code change cannot create or top up DeepSeek API balance; the account owner must resolve provider billing if that route is needed.

### Prompt alignment

- The documentary prompt now explicitly instructs the planner to repeat at least half of the concrete `visual_focus` tokens verbatim in the primary `visual_query`.
- This aligns model output with the existing deterministic validator instead of weakening the validator after a mismatch.

## Validation performed

- `python -m py_compile *.py tests/*.py`: passed.
- `python -m unittest discover -s tests -v`: **71/71 passed**.
- Added regression tests for a same-provider semantic repair, fallback after a failed repair, JSON-mode requests, explicit HTTP 402 billing handling, and a combined failure sequence modelled on the failed Actions run (Groq schema mismatch → DeepSeek insufficient balance → Gemini 503 → OpenRouter malformed JSON → successful OpenRouter repair).
- `healthcheck.py` was also run: requests, CPU PyTorch, SentencePiece, protobuf, Pillow, safetensors, FFmpeg, ffprobe, libass, and libx264 passed. The local container does not have `edge_tts` or `transformers`; an installation attempt could not reach PyPI because DNS/network access is unavailable here. Both packages remain declared in `requirements.txt`, and the GitHub Actions workflow installs that file before the health check.
- ZIP extraction and full clean-extraction test run are performed as part of final packaging.

## Limitations and required user-side checks

- No live request was made using the user's API keys, and no new GitHub Actions render was executed from this environment.
- The observed DeepSeek insufficient-balance condition requires a balance/payment correction in the DeepSeek account; code cannot resolve it.
- The observed Gemini 503 condition is provider-side availability. The router already performs one bounded retry of transient server failures after trying the other configured routes.
- If all four providers remain unavailable or all reject the corrected plan, the generator still stops rather than using an unverified script. This preserves the project's factuality requirement.
- A successful plan-generation regression test is not a guarantee of a successful full documentary render; live fact-check, TTS, footage, pixel verification, and FFmpeg steps still require a real Actions run.
