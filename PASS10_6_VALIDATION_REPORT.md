# Pass 10.6 Validation Report

## Failure addressed

The supplied Actions log `logs_122860196337.zip` confirms that the versioned ZIP was extracted and installed on branch `project1`. The workflow passed its dependency and unit-test stage (82 tests). The planner produced an 189-word, 8-scene documentary plan. The run stopped in fact verification before voice-over or video generation because quantitative claim C1 had a relevant fetched passage from only one distinct trusted source organisation; the policy requires two.

The log separately shows provider instability: Groq received HTTP 429, DeepSeek reported HTTP 402 insufficient balance/payment, and Gemini returned service/timeout errors. The planner nevertheless succeeded through Groq, and the hard stop shown in the traceback was the insufficient independent evidence for C1, not an API-key-missing error or an FFmpeg/rendering error.

## Changes

- Quantitative claims may enter evidence retrieval with one or more candidate URLs; they are not approved with one source. The two-organisation rule is enforced after the pages are fetched and relevant evidence excerpts are assembled.
- If a claim has no relevant fetched page, or a quantitative claim has fewer than two relevant independent organisations, the verifier makes one bounded request for up to two additional direct source-page candidates.
- Recovered URLs must be HTTPS, pass the institutional allowlist, come from organisations not already tried, and be distinct from each other. They are candidates only, not evidence.
- Every recovered page must be fetched directly, pass safe redirect checks, and produce a claim-relevant excerpt from its actual page text. The separate fact-audit model must still support each exact claim from those excerpts.
- A global 24-page retrieval ceiling remains in force, including recovered URLs. If recovery does not establish the required evidence, the run still fails closed before TTS/render.
- Failure logs now print the exact blocked narration sentence, recovery provider/candidate result, and recent per-source diagnostics, while `fact_check_report.json` retains the detailed structured report.

## Local validation

- `python -m py_compile main.py ai_router.py visual_matcher.py visual_fallback.py healthcheck.py fact_check.py`: passed.
- `python -m unittest discover -s tests -q`: **84 tests passed**.
- Recovery tests confirm that one relevant initial source can be supplemented by another trusted organisation, and subdomains of an already-tried organisation are rejected as non-independent.
- ZIP integrity: checked during release packaging.

## Not verified

No live provider call, live source-page retrieval, GitHub Actions run, or full MP4 render was performed in this environment. Source-recovery still depends on provider availability and on the existence of readable, trusted pages that directly support the exact sentence. DeepSeek's HTTP 402 balance condition and external Groq/Gemini outages must be resolved by provider availability/account state; code cannot replenish provider quota or balance.
