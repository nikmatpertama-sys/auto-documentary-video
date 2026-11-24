# Pass 10.4 Validation Report

## Failure addressed

The supplied GitHub Actions log `logs_122841188970.zip` showed that planning completed successfully
(8 scenes, 189 words), then the run stopped before narration/voice-over at factual-source validation:

`Fact claim 1 includes a non-trusted or non-HTTPS source URL.`

This was a different failure from the earlier scene-count issue. The latest run did not reach voice-over or rendering.

## Changes

- Added trusted-source URL preflight to the planner validator, before the AI router accepts a plan.
- Invalid URL schemes/hosts now raise a structured validation error that triggers the router's existing one-time same-provider repair.
- If repair remains invalid, normal provider failover continues; the source policy remains fail-closed.
- The runtime fact-checker reuses the same URL validator and reports the offending scheme/hostname without logging full query strings.
- Removed the invalid `.example` URL from the planner's JSON example and clarified HTTPS/approved-host requirements.
- Did not disable factual evidence checks, allow arbitrary domains, lower visual thresholds, or bypass video verification.

## Local tests

- `python -m unittest discover -s tests -v`: **80 tests passed**.
- `python -m compileall -q .`: passed.
- ZIP integrity check: passed.

## Not verified

A live GitHub Actions run, live AI provider repair, live trusted-source fetch, and full video render were not run in this environment. Those require the user's GitHub Actions execution and provider/service availability.
