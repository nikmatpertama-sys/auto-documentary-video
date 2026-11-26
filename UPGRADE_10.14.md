# Upgrade 10.14 — API rotation, Speechify, and 70s+ production

## Root cause in the latest log

The 26 November 2026 build stopped during visual verification, not during the final render. Groq returned HTTP 429 for its token-per-minute limit, OpenRouter's free-model daily quota was exhausted, and Gemini returned HTTP 503. The existing fallback pool had only one key per provider, no Cloudflare Vision route, and no way to rotate through multiple credentials for the same provider.

## Changes

### 1. API key auto-discovery and rotation
- Detects base secret names plus numbered variants for Groq, OpenRouter, and Gemini.
- Supports Cloudflare credential pairs `CLOUDFLARE_API_TOKEN` + `CLOUDFLARE_ACCOUNT_ID` and `CF_API_TK1` + `CF_ID1`, with numbered pairs available for future backups.
- Adds Cloudflare Workers AI text and Llama 3.2 Vision routes.
- On 429, 402, authentication, and transient service failures, the current credential/route is cooled down or disabled and the next available credential/provider is attempted.
- OpenRouter routes sharing an exhausted key are cooled down together, avoiding repeated requests to another free model with the same exhausted daily quota.
- Visual verification remains strict. If every configured provider is exhausted or unavailable, the scene is not silently approved.

### 2. Speechify as primary VO
- Uses `SPVO_API` first, with `SPEECHIFY_API_KEY` accepted as an alternate/numbered key name.
- Uses Speechify `simba-3.5`, searches paginated voice-catalog results for a male voice with calm/deep/baritone/documentary descriptors, rotates catalog lookup across configured Speechify keys, and accepts `SPEECHIFY_VOICE_ID` as an explicit override.
- Applies a calm, lower-pitched baseline and subtle prosodic variation for the opening hook, questions, and ending.
- Uses Speechify word speech marks for caption timing; if marks are absent, it estimates timings from the exact spoken sentence.
- If Speechify cannot finish the complete script, it discards partial Speechify assets and restarts the full narration with the existing Edge TTS fallback. This prevents mixed voices in one video.

### 3. Duration
- The narration hard limit is 120–200 spoken words.
- The final video must be at least 70 seconds.
- The complete rendered duration follows the full VO timeline. Durations above 90 seconds are allowed; no max-duration cap is applied.
- If the complete voice track is shorter than 70 seconds, the pipeline pads only the end with silence to meet the minimum.
- The final mux is checked against both the 70-second minimum and the full voice timeline.

### 4. GitHub Actions secret mapping

The archive intentionally does not replace `.github/workflows/render.yml`. Add the following to the existing job-level or step-level `env:` section, preserving other environment variables:

```yaml
CLOUDFLARE_API_TOKEN: ${{ secrets.CLOUDFLARE_API_TOKEN }}
CLOUDFLARE_ACCOUNT_ID: ${{ secrets.CLOUDFLARE_ACCOUNT_ID }}
CF_API_TK1: ${{ secrets.CF_API_TK1 }}
CF_ID1: ${{ secrets.CF_ID1 }}
SPVO_API: ${{ secrets.SPVO_API }}
```

Map any additional numbered provider keys in the same fashion. Repository secrets are not made available to the Python process unless the workflow exposes them through `env:`.

**Cloudflare Vision note:** the default Llama 3.2 Vision model can require a one-time Meta license/AUP acceptance in Cloudflare. The workflow will not accept legal terms automatically. Complete that provider setup manually if Cloudflare returns an activation/license error. This does not prevent other configured providers from being attempted.

## Validation status

- Static syntax and unit checks are run against the final files before packaging.
- No real provider credentials are available in this patch environment, so live Speechify/Cloudflare generation cannot be authenticated here.
- No existing GitHub Actions workflow is bundled or overwritten.
