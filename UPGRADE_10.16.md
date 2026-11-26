# Upgrade 10.16 — Visual provider recovery and Cloudflare diagnostics

## Fixed from the latest failed run

- Visual verification now re-queues temporarily rate-limited routes and retries them after the provider/quota cooldown, with a bounded per-route attempt count and overall wait budget.
- Gemini HTTP 5xx and transient request timeouts receive a bounded retry instead of exhausting the route after a single attempt.
- OpenRouter's account-wide free-model daily cap remains non-retryable for the current run; sibling free keys/models are not pointlessly retried.
- Cloudflare 401/403 responses are still treated as configuration/authorization errors, but the response detail is now included in sanitized logs to distinguish token permissions, model entitlement, and license acceptance.
- Provider error logging redacts configured API secrets.

## Existing behavior preserved

- Script target remains 120–200 words.
- Voice-over stays Speechify-first using `SPVO_API`; Edge TTS remains fallback only.
- Final video remains at least 70 seconds and follows the complete voice-over, with no 90-second maximum.
- Visual verification remains required by default. The update does not silently downgrade to metadata-only footage verification.
- The 10.15 quota router and its account/project quota grouping are retained.

## Required Cloudflare setup

The current Cloudflare vision route uses `@cf/meta/llama-3.2-11b-vision-instruct`. Cloudflare's model documentation requires an initial request with `{"prompt":"agree"}` to accept Meta's License and Acceptable Use Policy. The API token must also carry a Workers AI permission (Workers AI Read or Workers AI Write). This project does not automatically accept third-party model terms on the user's behalf.

## Validation

- Python syntax checks pass.
- Existing quota-router tests pass.
- Added retry-policy tests cover Groq 429 cooldown retry, Gemini 503 retry, OpenRouter daily-cap no-retry, Cloudflare 403 no-retry, bounded retries, and log redaction.
