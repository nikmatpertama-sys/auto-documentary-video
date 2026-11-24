# Pass 10.5 Validation Report

## Failure addressed

The latest supplied Actions log (`logs_122852792294.zip`) showed that planning succeeded (8 scenes, 167 words), but factual verification stopped during local evidence-excerpt selection with `Fetched source page has no sufficiently relevant passage for this exact claim.` No TTS or video rendering began.

## Changes

- A source that fetches successfully but has no usable passage is now recorded as `not_usable`; the verifier continues to the remaining supplied URLs instead of aborting at the first candidate.
- A claim is still blocked if no supplied trusted source produces an evidence excerpt.
- Quantitative claims still require relevant excerpts from two distinct trusted source organizations; two URLs alone do not satisfy that requirement.
- The local excerpt selector now permits a single lexical anchor to be sent to the independent semantic fact auditor. The AI auditor must still judge the complete claim as supported from the fetched excerpt; it cannot use prior knowledge or invent evidence. Zero-anchor pages remain unusable.
- The planner prompt now asks for two genuine source URLs where available, while explicitly prohibiting invented citations.
- The workflow ZIP selector now recognises versioned `project1-10.x.zip` releases as well as legacy upgrade ZIP names.

## Local validation

- `python -m unittest discover -s tests -q`: **82 tests passed**.
- `python -m compileall -q .`: passed.
- ZIP integrity check: performed at release packaging.

## Not verified

A live GitHub Actions run, live provider responses, live trusted-source retrieval, and a full video render were not performed in this environment. The updated workflow selector must be present on the active `project1` branch before the new filename can be detected.
