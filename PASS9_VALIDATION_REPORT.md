# Pass 9 validation report

## Diagnosis

The Pass 8 run reached the visual retrieval stage and correctly rejected footage that did not match the requested `rainfall map` / `precipitation map` graphics. The scene was a natural-process story beat, but the generated query set made a published data map the expected asset. As a result, normal footage of rain, forest, and rivers was rejected by pixel-level Vision checks. Repeated candidate checks then exhausted the run's limited external Vision-provider availability.

## Pass 9 changes

1. Made real footage the explicit default for filmable natural processes, including rainfall, evaporation, transpiration, cooling, clouds, river flow, and ecology.
2. Added evidence gating to trusted-reference visuals. Map/chart/diagram language alone is no longer enough; the narration must require concrete quantified/geographic evidence or an internal scientific structure.
3. Required the planner's primary and alternate visual queries to describe real footage; the reference query is separate.
4. Sanitized reference-artifact terms from stock-footage search phrases. A failed reference lookup therefore falls back to searchable real-world subjects, not more map/diagram searches.
5. Added real footage query expansions for rainforest rainfall and canopy shots.
6. Interleaved the primary and two alternative shot searches so the bounded eight-query budget cannot be consumed by expansions of the primary query alone.
6. Kept valid Vision REJECT decisions, the 0.72 confidence threshold, and the conservative metadata outage policy unchanged.

## Local validation

- Python compile check: passed.
- Unit tests: 50/50 passed after adding a regression for interleaving the primary and two independent shot queries.
- Regression tests: generic rainfall-map request is treated as footage; measured geographic rainfall data still requests a trusted reference; map/chart terms are removed from stock-footage queries; rainforest rainfall produces concrete live-footage variants; independent shot alternatives each receive a search slot.

## Not verified

This package has not been executed in the user's live GitHub Actions environment. Local tests do not validate current Pixabay/Commons downloads, external API quotas, or the final MP4. Code cannot restore provider balance or bypass HTTP 429 rate limits. When all Vision APIs are unavailable, the existing strict metadata-only fallback may still reject a scene if its source metadata is not sufficiently specific; this is intentional rather than accepting unrelated footage.
