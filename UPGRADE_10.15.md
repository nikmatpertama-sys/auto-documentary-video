# Upgrade 10.15 — quota-aware provider routing

## What changed from 10.14

10.14 could rotate to another credential after an error, but it did not compare currently available quota before choosing a provider. 10.15 adds `quota_router.py` and reranks eligible provider routes before each documentary-plan or footage-visual-verification request.

## How capacity is measured

| Provider | What the automation can check | Quota scope used by the router |
|---|---|---|
| OpenRouter | Live `GET /api/v1/key` response for per-key spend headroom; parses `error.metadata.limit_source` from inference errors | Per key for spend caps; per account for free-model RPM/RPD and credits/in-flight budgets; per model for upstream shared-pool throttles |
| Groq | Remaining requests/tokens and limit values from the inference response headers | Organization (shared by default) |
| Gemini | Quota status and retry delays from actual API errors; exact live remaining quota is not exposed by a general per-key endpoint | Google project (shared by default) |
| Cloudflare Workers AI | Actual success, HTTP 402/429, and any retry hints; no general live remaining percentage | Cloudflare account ID |
| Speechify | Actual HTTP 402/429 and `Retry-After`; current documented synthesis limits are per account, not per key | Speechify account (shared by default) |

Where remaining quota is measurable, the router ranks by the lowest remaining ratio across the available dimensions. Where it is not measurable, it prints `UNKNOWN` and uses a neutral score plus least-used/least-recently-used balancing. It does not pretend that an unknown quota is 100% available.

## Scope safety

- OpenRouter keys share one account-level pool by default for free-model limits. `openrouter_free_tier_per_minute` cools every free-model route on that account; `openrouter_free_tier_daily` makes free-model routes unavailable for the remainder of the current workflow run; `upstream_provider_shared_pool` cools only the matching model; `openrouter_key_limit` affects only that key; and `openrouter_credits` / `openrouter_in_flight_budget` affect the account pool. Use `OPENROUTER_QUOTA_GROUP_1`, `OPENROUTER_QUOTA_GROUP_2`, etc. only when keys genuinely belong to different OpenRouter accounts. Keys from the same account must share a group label.
- Groq limits are organization-level, so all Groq keys share a quota pool by default. Only configure separate `GROQ_QUOTA_GROUP_1`, `GROQ_QUOTA_GROUP_2`, etc. when you know those keys belong to different organizations.
- Gemini limits are project-level, so all Gemini keys share one conservative pool by default. For separate Google projects, map `GEMINI_PROJECT_ID_1`, `GEMINI_PROJECT_ID_2`, etc. to the corresponding key slots, or use one shared `GEMINI_PROJECT_ID` when every key belongs to the same project.
- Cloudflare credentials are grouped by the account ID paired with each token. Two tokens from the same account do not create two independent Workers AI quota pools.
- Speechify rate and concurrency limits are shared across API keys belonging to the same account. A 429 or account balance error does not cause pointless same-account key rotation; the full-script Edge TTS fallback is used when Speechify cannot complete.

## GitHub Actions workflow

No new mandatory API secret is required for this upgrade. Keep all 10.14 mappings already added to the existing workflow, including:

```yaml
CLOUDFLARE_API_TOKEN: ${{ secrets.CLOUDFLARE_API_TOKEN }}
CLOUDFLARE_ACCOUNT_ID: ${{ secrets.CLOUDFLARE_ACCOUNT_ID }}
CF_API_TK1: ${{ secrets.CF_API_TK1 }}
CF_ID1: ${{ secrets.CF_ID1 }}
SPVO_API: ${{ secrets.SPVO_API }}
```

Continue mapping any numbered provider API keys as needed. Optional scope variables can be added only when the keys genuinely belong to separate organizations/projects; do not invent separate IDs for keys sharing one account. For example, only when OpenRouter keys belong to separate accounts, add ordinary workflow environment labels such as `OPENROUTER_QUOTA_GROUP_1: account-a` and `OPENROUTER_QUOTA_GROUP_2: account-b`. These are labels, not API secrets. Keys on the same account must use the same label (or leave the default shared group unchanged).

## Validation

- Offline unit tests cover Groq header ranking, OpenRouter per-key headroom, account-level free-tier caps, model-specific upstream cooldowns, account credits, in-flight/key spending limits, account/project shared cooldowns, credential-specific authentication failures, and explicit unknown-quota handling.
- Python syntax compilation is run for all Python modules.
- No real API keys are present in this patch environment, so live provider quota endpoints and a complete GitHub Actions render cannot be authenticated here.
