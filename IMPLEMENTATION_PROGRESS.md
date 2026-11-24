# Implementation Progress — Repair Roadmap

Working branch: `freebuff/ef979e0f170b30dd82bd5b84` (based on `project1` at `9f244a8`).
Delivery target: `project1`. `main` was never checked out or modified.

Recovery checkpoint created before any code change:

- branch `backup/project1-9f244a8` at `9f244a8`
- tag `checkpoint/pre-implementation-2026-11-24`
- `git bundle` of all refs: `.backups/project1-full-9f244a8.bundle` (gitignored, local only)

## Completed phases

| Phase | Scope | Commit |
| --- | --- | --- |
| 0 | Failure diagnostics, stage timing, always-on artifacts, workflow safety | `e7f102f` |
| 1 | Opening gate before TTS, audio/video mux padding, duration gate, credits bug, font bundling | `e7f102f` |
| 2 | AI routing: 429 cooldown instead of permanent disable, Retry-After, DeepSeek vision gating | `3390d0b` |
| 3 | Fact-check page budget + fetch retry; footage relevance/static-clip rejection; `main.py` test suite | `69f2e45` |
| 4 | Frozen-tail mux fix, picture-track validation, offline render harness, workflow permissions, healthcheck, docs | `f22c0ad` |
| 5 | Narration tempo floor widened so a legitimate 150–160 word script cannot fail after TTS is paid | (this commit) |

## Modified / added files

- `diagnostics.py` (new) — stage timings, redacted failure reports, environment summary.
- `main.py` — stage instrumentation, `prepare_opening`, `format_scene_credit`, `static_clip_ratio`,
  `FFMPEG`/`FFPROBE` overrides, final mux padding, stricter `validate_final_video`, Pexels HD renditions,
  deeper audio chain (70 Hz high-pass, 7.5 kHz −3 dB shelf, 13.5 kHz low-pass).
- `ai_router.py` — bounded provider cooldowns, `Retry-After`, quota-vs-rate-limit distinction,
  DeepSeek vision opt-in, per-task health isolation retained.
- `fact_check.py` — page budget derived from claim count, one bounded retry for transient source failures.
- `narration_timing.py` — `MIN_SAFE_ATEMPO` 0.85 → 0.80.
- `hook_overlay.py` — bundled-font lookup (`HOOK_FONT_PATH`, `assets/fonts`, system paths), loud failure
  instead of an unreadable bitmap fallback.
- `healthcheck.py` — overlay font check, free-disk check.
- `.github/workflows/render.yml` — `contents: read`, no self-commit/push step, `timeout-minutes: 120`,
  `pip check` + `pip freeze`, `tee output/pipeline.log`, failure artifacts (`if: failure() || cancelled()`),
  failure summary in the job log, vision env forced to `true`/8 candidates.
- `tests/test_main_pipeline.py` (new), `tests/test_ai_router.py`, `tests/test_narration_timing.py`.
- `scripts/offline_render_check.py` (new), `assets/fonts/DejaVuSans*.ttf` (bundled), `README.md`,
  `THIRD_PARTY_NOTICES.md`, `.gitignore`.

## Tests executed and actual results

- Baseline before changes: `Ran 84 tests`, `FAILED (failures=2, skipped=2)` — the two failures were the
  missing-font opening-card tests, the two skips needed FFmpeg.
- Final: `Ran 112 tests`, `OK` (FFmpeg 8.0.1 present locally, so the media tests run rather than skip).
- `python -m py_compile` on `main.py`, `ai_router.py`, `diagnostics.py`, `fact_check.py`,
  `hook_overlay.py`, `narration_timing.py`, `healthcheck.py`: exit 0.
- Workflow YAML parses; 13 steps; `permissions: contents: read`.

## Actual render evidence

`python scripts/offline_render_check.py --preset medium` (real FFmpeg, real Edge TTS, placeholder footage,
no paid API calls), evidence in `output/offline_render_evidence.json`:

- narration: 77.96 s, 191 words, 191/191 words Edge-timed (100 %), 33 caption groups.
- picture: h264 1080×1920, 30 fps, 2339 frames, 77.966 s.
- audio: aac 48 kHz stereo, 77.981 s, mean −20.1 dB, peak −1.4 dB (no clipping).
- duration delta container vs narration: +0.017 s.
- render time: 120.6 s on 2 vCPU.
- frames inspected: `output/evidence/frame_hook.png` shows the validated opening card and the karaoke
  subtitle burned into the picture.

Not verified: relevance of real stock footage, provider Vision verdicts, fact-check against live sources,
and a complete GitHub Actions run — all of those need the real API keys and a workflow run.

## Remaining problems / known limitations

- A real end-to-end Actions run has not been executed (needs the user's secrets and explicit permission
  for the paid provider calls). Everything up to that point is verified locally.
- Footage search/media caches still carry a 6-hour TTL; a stale signed media URL consumes one candidate
  attempt before failover (bounded, not fatal).
- Dead modules `voice_pipeline_block.py`, `visual_outage_policy.py`, `visual_fallback.py` remain and are
  still covered by tests; they are intentionally not deleted (no silent functionality removal).
- `requirements.txt` uses compatible ranges without a lockfile; the workflow now records `pip freeze` and
  runs `pip check` per run instead of pinning blindly.
- `healthcheck.py` cannot run locally here because CPU PyTorch/Transformers are not installed in the
  sandbox; CI installs them before the check.

## External service / billing requirements

- AI text + fact-check: one of `GROQ_API_KEY`, `DEEPSEEK_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`.
- Vision (mandatory): at least one multimodal provider key (Groq/Gemini/OpenRouter; DeepSeek only with
  `DEEPSEEK_VISION_MODEL`).
- Footage: at least one of `PIXABAY_API_KEY`, `PEXELS_API_KEY`, `COVERR_API_KEY`; NASA and Wikimedia
  Commons need no key.
- Edge TTS is free and was reachable from this sandbox.
