# Real render validation (free toolchain, no paid API)

This run proves the production render path produces a playable vertical documentary
film on a clean Linux machine with no provider keys and no paid service.

## What was executed

| Stage | How it was produced | Result |
| --- | --- | --- |
| Narration | `main.build_voiceover()` with free Edge TTS (`en-US-GuyNeural`, `-4%`, `-3Hz`) | 77.964 s of audio, 191/191 words boundary-timed by Edge (`alignment_status: complete_edge_timing`) |
| Captions | `main.build_subtitles_from_voice()` then the `subtitles=` burn-in in `main.render()` | 33 phrase groups, karaoke `\kf` tags, visible in the encoded frames |
| Opening card | `main.prepare_opening()` + `hook_overlay` | Hook card rendered over scene 1 |
| Visuals | 8 locally generated 1080 × 1920 clips (placeholder footage, no stock API) | Every scene decoded and encoded |
| Render | `main.render()` at the production profile (`medium` x264, CRF 20) | 119.6 s wall clock on 2 vCPUs |
| Audio chain | highpass 70 Hz → lowpass 13.5 kHz → 7.5 kHz −3 dB dip → compressor → limiter → `loudnorm I=-16 TP=-1.5` | mean −20.1 dB, true peak −1.5 dBFS, no clipping |
| Validation | `main.validate_final_video()` + `ffprobe` | duration delta 0.005 s against the locked VO timeline |

Command:

```bash
FFMPEG_BIN=$HOME/ffmpeg/ffmpeg FFPROBE_BIN=$HOME/ffmpeg/ffprobe \
  python scripts/offline_render_check.py --preset medium
```

## Measured output (`documentary.mp4`)

```json
{
  "format": {"duration": "77.969000", "size": 40107146, "bit_rate": "4115188"},
  "streams": [
    {"index": 0, "codec_name": "h264", "width": 1080, "height": 1920,
     "r_frame_rate": "30/1", "duration": "77.966667"},
    {"index": 1, "codec_name": "aac", "sample_rate": "48000", "channels": 2,
     "duration": "77.969000"}
  ]
}
```

Full evidence file: `output/offline_render_evidence.json` (written by the run).

## Unit and integration tests

`python -m unittest discover -s tests -q` — 150 tests, all passing, no skips, with
FFmpeg, CPU PyTorch, transformers, sentencepiece, protobuf and safetensors installed.

`python healthcheck.py` — passed (FFmpeg subtitles filter, libx264, bundled font, model stack).

## What this run does not cover

The generative stages (source-pool research, script planning, fact-check audit and
claim repair, stock-footage retrieval, vision verification) call free-tier AI and
stock APIs and require at least one provider key. This machine has no provider
credentials configured, so those stages were not executed here and no result is
claimed for them. They are covered by unit and mocked end-to-end tests only.
Running the full pipeline requires, for example, a free Groq, Gemini or OpenRouter
key in `GROQ_API_KEY`, `GEMINI_API_KEY` or `OPENROUTER_API_KEY`; the workflow keeps
`AI_ALLOW_PAID_PROVIDERS=false` so no paid route can be selected.
