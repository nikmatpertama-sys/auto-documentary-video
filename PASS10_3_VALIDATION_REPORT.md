# Pass 10.3 validation report — scene-count recovery

## Failure evidence

The 2026-11-24 Actions log shows plan generation failed because Groq returned a scene list whose length was not exactly eight. The same-provider repair also returned a non-eight-scene plan. The remaining text routes then failed independently: DeepSeek HTTP 402 (insufficient balance), Gemini HTTP 503 (high demand), OpenRouter dynamic free route with `finish_reason=length` and empty assistant content, and the explicit OpenRouter ERNIE fallback with HTTP 429. The workflow stopped before fact verification, footage retrieval, or MP4 rendering.

## Change

- Added `scene_normalization.py` to perform conservative, deterministic scene-count normalization.
- A scene may be split only at a sentence boundary when both sentences independently contain at least half of the scene's visible-focus terms.
- Adjacent scenes may be merged only when both contain one sentence and their visual-focus terms, primary footage-query terms, and visual modes materially overlap. Trusted-reference scenes additionally require the same reference query and source URL.
- No narration is invented or removed. Every fact-claim scene index is remapped; if any claim cannot be mapped unambiguously, normalization is refused.
- `main.py` still requires exactly eight scenes and retains the existing word-count, sentence-count, evidence-ledger, source, and visual-focus checks. Unsafe normalizations still fail closed and use existing provider-repair/failover logic.

## Local checks

- `python -m py_compile *.py tests/*.py`: passed.
- `python -m unittest discover -s tests -q`: **77/77 passed**.
- New regression coverage: safe 7-to-8 split, safe 9-to-8 merge, remapping of evidence claims, refusal to merge unrelated subjects, and refusal to invent missing narration.

## Not verified

No live Groq, DeepSeek, Gemini, or OpenRouter call was made; no real source pages or stock-footage endpoints were requested; and no GitHub Actions render or final MP4 was produced. The patch fixes a software-side plan-structure failure when a safe transformation exists. It cannot replenish API balance or bypass upstream 503/429 responses. If all configured text providers are unavailable, the run can still stop with a clear provider error rather than generate an unsupported script.
