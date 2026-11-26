# Auto Documentary Video — Version 10.10

Automated vertical documentary generator. It builds a scene plan, searches for relevant real-world footage, performs visual verification, generates narration and captions, and renders the final video with FFmpeg.

## Master scriptwriter prompt

`SCRIPTWRITER_MASTER_PROMPT.md` remains the single source of truth for the generator's professional documentary scriptwriter role. `main.py` loads it at runtime through `documentary_prompt()`. The prompt enforces factual discipline, an eight-scene structure, a 145–165-word narration target, and practical footage queries. Its claim-by-claim audit is part of the same prompt; it does not create a second independent fact-check API call.

## Footage source selection — version 10.10

The active pipeline (`render.py` → `main.py`) enforces this **selection** priority:

1. Internet Archive
2. Wikimedia Commons
3. NASA
4. Pexels
5. Pixabay

Candidate relevance is ranked **within each source tier**, never globally across all providers. The default visual-check candidate budget is 10 Internet Archive candidates and up to 4 candidates from each fallback source. The generator attempts the Internet Archive tier first; it proceeds to Wikimedia Commons, NASA, Pexels, and Pixabay only when candidates in earlier tiers fail to download or fail visual verification.

Important distinction: the existing search collector still gathers metadata from configured sources before candidate selection. This upgrade enforces which source's clips are attempted first; it does not yet make fallback API searches lazy. Pexels and Pixabay are fallback providers, not the top-ranked selection source.

Internet Archive results are admitted only when metadata explicitly indicates CC0, CC BY, or Public Domain. Wikimedia Commons results are license-filtered. NASA assets may require checking third-party credits. Always verify the exact source-page license and attribution requirements before publishing.

`footage.py` is a legacy standalone Pixabay utility and is not called by the active `render.py` → `main.py` pipeline. Use the integrated pipeline for documentary production.

## GitHub Actions workflow preservation

This ZIP intentionally **does not include** `.github/workflows/render.yml`. The bundled 10.9 workflow is older and would risk overwriting the newer permanent-apply workflow already configured in the target repository. Keep the updated workflow on your branch; this archive only overlays the project files it contains and does not alter that workflow.

## Required GitHub Secrets

Add at least one AI provider key: `GEMINI_API_KEY`, `GROQ_API_KEY`, or `OPENROUTER_API_KEY`. `PIXABAY_API_KEY` and `PEXELS_API_KEY` are optional fallback-provider keys. Internet Archive, Wikimedia Commons, and NASA searches do not require API keys.

## Output

The pipeline produces `documentary.mp4`, `plan.json`, `credits.txt`, `timing.json`, and the voice-over/caption files.

## Version 10.10 — source-priority enforcement

- Enforces source-tier selection: Internet Archive → Wikimedia Commons → NASA → Pexels → Pixabay.
- Ranks relevance only within each source, so a high Pexels/Pixabay relevance score cannot outrank an available Internet Archive candidate.
- Gives Internet Archive a larger candidate-check budget than each fallback source.
- Retains the existing visual verification and download-failure fallback behavior.
- Keeps the 10.9 single-role scriptwriter master prompt unchanged and loaded at runtime.
- Does not add a second LLM/API request for fact-checking.
- Omits the outdated bundled `render.yml` so the target repository's updated permanent workflow is preserved.
