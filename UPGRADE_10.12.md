# Project 1 — Version 10.12 upgrade notes

## Fixes based on the 10.11 run

- Handles Wikimedia Commons media-download HTTP 429 responses, not only Commons API search throttling.
- Applies one shared request interval to Commons API requests and `upload.wikimedia.org` media downloads.
- On the first Wikimedia media-download 429, honors `Retry-After` when supplied, removes any partial file, and starts a timed source cooldown. Remaining pre-ranked Commons candidates are skipped during that cooldown, allowing NASA/Pexels/Pixabay fallback tiers to continue without hammering the blocked host.
- Uses the number of candidates remaining after strict per-source ranking caps to determine whether a scene needs more searches. A large raw metadata pool can no longer suppress fallback searches when the selectable shortlist is still sparse.
- Raises the candidate caps for NASA, Pexels, and Pixabay to eight each while retaining the 26-candidate total limit and the existing strict tier order.
- Adds distinct query alternatives for climate-model simulations, atmospheric data visualizations, and scientists analyzing climate data, while retaining separate fieldwork and instrument searches.
- Generic forest queries now require matching forest/tree metadata; weak overlap with unrelated titles is not enough to admit a candidate.

## Preserved behavior

- Strict source selection priority remains Internet Archive → Wikimedia Commons → NASA → Pexels → Pixabay.
- Relevance is ranked only within each source tier.
- License checks, direct downloads, mandatory visual verification, confidence threshold, and related-scene re-verification remain enabled.
- No unrelated footage, metadata-only shortcut, or unverified diagram is inserted to force a successful render.
- `SCRIPTWRITER_MASTER_PROMPT.md` is preserved byte-for-byte and remains loaded by `main.py`.
- Wikipedia is not added as a separate footage provider; Wikimedia Commons remains the video-media source.
- The outdated `.github/workflows/render.yml` is not included, so this ZIP does not replace the user's existing workflow.

## Validation

- Python syntax compilation.
- ZIP integrity and CRC checks.
- Unit-style checks for climate-model fallback query generation, forest metadata anchors, source-tier order/caps, Wikimedia 429 cooldown and candidate skipping, and master-prompt integrity.
- A live networked documentary render with the user's API credentials is not claimed by this package build.
