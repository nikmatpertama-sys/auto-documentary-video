# Pass 8 validation report

## Root cause from run 122692102959

Pass 7 was successfully extracted from `auto-documentary-video-upgrade(3).zip` and committed to `project1` as `043d10b`. The workflow and ZIP detection were working.

The render then failed at Scene 4 because every available visual-verification route became unavailable. The log shows that during script generation, DeepSeek reported insufficient balance and Gemini reported temporary high demand. Later, during visual verification, Groq returned HTTP 429; the primary OpenRouter model returned an unreadable response; and the OpenRouter fallback returned HTTP 429. The remaining configured routes did not get attempted.

The code-level cause was shared provider health state in `ai_router.py`: cooldowns, disabled routes, and disabled providers were keyed without the task type. A text-generation failure could therefore make the same provider/model appear unavailable to a later image-verification request. The log showed matching DeepSeek and Gemini model names for both tasks, which exposed this collision.

## Pass 8 changes

1. Isolate provider disable state by task (`text` versus `vision`).
2. Isolate route disable state by task.
3. Isolate cooldown timers by task, including bounded transient retries.
4. Keep the existing HTTP error handling, provider order, visual confidence threshold, metadata fallback policy, and strict pixel-level acceptance rules unchanged. A valid visual rejection still cannot be overridden by metadata.
5. Add a regression test that forces Gemini text requests into a transient cooldown, then confirms the same Gemini model remains available for a subsequent vision request.

## Local validation

- 46/46 unit tests passed.
- `ai_router.py` compiled successfully with `py_compile`.
- The regression test reproduced the previous cross-task cooldown condition and passed with the task-scoped state.

## Not verified

This package has not yet been run in the user's live GitHub Actions environment. The next Actions run is still required to verify live API quotas, model availability, external footage downloads, and the completed MP4. This patch makes the vision router try its own configured routes after text-provider failures; it cannot guarantee that external providers will be healthy or that every scene will have relevant footage.
