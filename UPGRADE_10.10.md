# Project 1 — Version 10.10 upgrade notes

## Included changes

- Enforces footage source selection priority: Internet Archive → Wikimedia Commons → NASA → Pexels → Pixabay.
- Ranks relevance only within each source tier. A candidate from a lower-priority source cannot outrank a candidate from a higher-priority source merely because it has a higher relevance score.
- Allocates up to 10 visual-check candidates to Internet Archive and up to 4 to each fallback source, subject to `FOOTAGE_VISION_CANDIDATE_LIMIT`.
- Moves to the next source tier only after candidates in the current tier fail to download or fail visual verification.
- Preserves the 10.9 professional documentary scriptwriter master prompt byte-for-byte and keeps its runtime loader in `main.py`.
- Retains the silent claim-by-claim accuracy audit in the prompt. This is not a separate web research/fact-check pass and does not add another LLM/API request.
- Keeps existing source license filtering, scene matching, visual verification, and related-scene reuse behavior.

## Search versus selection

The current collector still queries configured sources to build the metadata candidate pool. This release changes the **selection and verification order**. It does not yet defer Pexels/Pixabay API searches until the higher-priority tiers have failed.

## Workflow preservation

The outdated `.github/workflows/render.yml` bundled in 10.9 is intentionally omitted from this ZIP. Keep the newer permanent-apply workflow already configured in the target repository; this upgrade must not replace it with the older workflow.

## Validation

The package is checked for Python syntax, ZIP integrity, expected prompt presence, source-priority order, and the continued runtime loading of the master prompt.
