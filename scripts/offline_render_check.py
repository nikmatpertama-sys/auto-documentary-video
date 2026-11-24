#!/usr/bin/env python3
"""Offline end-to-end render check using the real production code path.

This script runs the actual pipeline functions — narration assembly, subtitle
alignment, scene timing, `render()` and `validate_final_video()` — against real
FFmpeg at the production profile (1080x1920, target 70-90s). It uses locally
generated placeholder footage instead of paid footage APIs, and real Edge TTS
narration when the free service is reachable (otherwise a synthesized stand-in
audio track, clearly labelled in the evidence file).

It writes output/offline_render_evidence.json with ffprobe measurements so the
result can be checked without watching the video.

Usage:
    python scripts/offline_render_check.py [--preset medium] [--keep-footage]
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

NARRATION = (
    "High above the valley floor, the forest wakes under a pale morning sky. "
    "Mist drifts between the trees while the first light reaches the leaves. "
    "Rain gathers slowly in the canopy and runs down every branch and stem. "
    "Each drop travels a long way before it reaches the soil below. "
    "Roots spread wide through the dark ground and hold the hillside together. "
    "Water moves through the wood in thin channels that never stop working. "
    "By midday the air is warm and heavy with moisture from the leaves. "
    "Clouds gather above the ridge and the light begins to soften again. "
    "When the rain finally arrives it falls steadily across the whole slope. "
    "Streams swell quietly and carry the water down toward the river. "
    "The forest keeps its own rhythm of rain, shade and slow evaporation. "
    "Nothing here is still, and nothing here is in a hurry. "
    "Evening settles over the canopy with a low and even light. "
    "The last water drips from the leaves into the waiting ground. "
    "By nightfall the air is cool and the valley is finally quiet. "
    "What began as mist returns to the sky as it always has."
)


def run(command, **kwargs):
    return subprocess.run(
        [str(part) for part in command],
        capture_output=True,
        text=True,
        check=True,
        **kwargs,
    )


def ffprobe_json(path):
    result = run([
        "ffprobe", "-v", "error", "-show_streams", "-show_format",
        "-of", "json", str(path),
    ])
    return json.loads(result.stdout)


def ensure_fontconfig():
    """Point fontconfig at the bundled fonts when the container has no config.

    libass fails to resolve "DejaVu Sans" without fontconfig, which would make
    the caption check meaningless on a bare machine.
    """
    if os.environ.get("FONTCONFIG_FILE") or Path("/etc/fonts/fonts.conf").is_file():
        return
    config_dir = Path(tempfile.mkdtemp(prefix="offline-fonts-"))
    config = config_dir / "fonts.conf"
    config.write_text(
        '<?xml version="1.0"?>\n<!DOCTYPE fontconfig SYSTEM "fonts.dtd">\n'
        "<fontconfig>\n"
        f"  <dir>{REPO_ROOT / 'assets' / 'fonts'}</dir>\n"
        f"  <cachedir>{config_dir / 'cache'}</cachedir>\n"
        "</fontconfig>\n",
        encoding="utf-8",
    )
    os.environ["FONTCONFIG_FILE"] = str(config)
    print(f"Using bundled fonts via {config}")


def build_placeholder_footage(main, count):
    """Create distinct, moving clips so every scene has real decodable video."""
    sources = (
        "testsrc2", "smptebars", "testsrc2", "smptehdbars",
        "testsrc2", "smptebars", "testsrc2", "smptehdbars",
    )
    clips = []
    for index in range(count):
        clip = main.FOOT / f"scene_{index + 1:02d}.mp4"
        source = sources[index % len(sources)]
        run([
            main.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi",
            "-i", f"{source}=size={main.W}x{main.H}:rate={main.FPS}:duration=6",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-pix_fmt", "yuv420p", str(clip),
        ])
        clips.append(clip)
    return clips


def synthesized_voiceover(main, sentences):
    """Stand-in narration when the free TTS endpoint is unreachable.

    Produces one PCM segment per sentence with the same gaps and lead/hold the
    real pipeline uses, so subtitle estimation and mux timing are still tested.
    """
    parts = []
    opening = main.OUT / "opening_silence.wav"
    run([
        main.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
        "-t", str(main.OPENING_LEAD), "-c:a", "pcm_s16le", str(opening),
    ])
    parts.append(opening)
    cursor = main.OPENING_LEAD
    records = []
    for index, sentence in enumerate(sentences):
        duration = max(2.2, len(sentence.split()) / 2.55)
        part = main.OUT / f"tts_{index:02d}.wav"
        run([
            main.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=140:sample_rate=48000:duration="
            f"{duration:.3f}",
            "-af", "volume=0.18,apad=pad_dur=0.05",
            "-ac", "2", "-c:a", "pcm_s16le", str(part),
        ])
        part_duration = main.ffprobe_duration(part)
        records.append({
            "sentence_index": index,
            "text": sentence,
            "start_unscaled": cursor,
            "end_unscaled": cursor + part_duration,
            "duration_unscaled": part_duration,
            "start": cursor,
            "end": cursor + part_duration,
            "duration": part_duration,
            "boundaries": [],
            "boundary_count": 0,
        })
        parts.append(part)
        cursor += part_duration
        gap = main.OUT / f"gap_{index:02d}.wav"
        run([
            main.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-t", str(main.SENTENCE_GAP), "-c:a", "pcm_s16le", str(gap),
        ])
        parts.append(gap)
        cursor += main.SENTENCE_GAP

    parts.pop().unlink(missing_ok=True)
    ending = main.OUT / "ending_hold.wav"
    run([
        main.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
        "-t", str(main.ENDING_HOLD), "-c:a", "pcm_s16le", str(ending),
    ])
    parts.append(ending)

    concat = main.OUT / "voice_concat.txt"
    concat.write_text(
        "".join(f"file '{part.resolve()}'\n" for part in parts), encoding="utf-8"
    )
    voice_path = main.OUT / "voiceover.wav"
    run([
        main.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(concat),
        "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", str(voice_path),
    ])
    total = main.ffprobe_duration(voice_path)
    mp3 = main.OUT / "voiceover.mp3"
    run([
        main.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(voice_path), "-c:a", "libmp3lame", "-b:a", "192k", str(mp3),
    ])
    return {
        "audio_wav": str(voice_path),
        "audio_mp3": str(mp3),
        "sentences": records,
        "sentence_events": [
            {"start": r["start_unscaled"], "end": r["end_unscaled"],
             "duration": r["duration_unscaled"]}
            for r in records
        ],
        "original_duration_seconds": total,
        "time_scale": 1.0,
        "total_duration": total,
        "voice": "synthetic-140Hz",
        "rate": "n/a",
        "pitch": "n/a",
    }


def build_plan(main):
    from sentence_utils import split_sentences

    sentences = split_sentences(NARRATION)
    if len(sentences) % 8:
        raise SystemExit(f"Narration must split into a multiple of 8 sentences, got {len(sentences)}")
    per_scene = len(sentences) // 8
    scenes = []
    for index in range(8):
        chunk = sentences[index * per_scene:(index + 1) * per_scene]
        scenes.append({
            "narration": " ".join(chunk),
            "visual_focus": f"placeholder scene {index + 1}",
            "visual_query": f"placeholder scene {index + 1}",
            "visual_queries": [f"placeholder alt {index + 1}a", f"placeholder alt {index + 1}b"],
            "visual_type": "footage",
        })
    plan = {
        "title": "Offline Render Verification",
        "hook": "This is an offline render check.",
        "script": " ".join(sentences),
        "scenes": scenes,
        "fact_claims": [],
    }
    main.assign_scene_sentence_ranges(plan)
    return plan


def main_cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preset", default="medium", help="x264 preset (production default: medium)")
    parser.add_argument("--no-tts", action="store_true", help="skip Edge TTS and use stand-in audio")
    parser.add_argument("--keep-footage", action="store_true")
    args = parser.parse_args()

    os.environ.setdefault("INPUT_TOPIC", "Offline render verification")
    os.environ.setdefault("TTS_VOICE", "en-US-GuyNeural")
    os.environ.setdefault("TTS_RATE", "-4%")
    os.environ.setdefault("TTS_PITCH", "-3Hz")
    os.chdir(REPO_ROOT)

    import main  # imported after environment setup, like the workflow does

    main.X264_PRESET = args.preset
    evidence = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "profile": {"width": main.W, "height": main.H, "fps": main.FPS, "preset": args.preset},
        "paid_api_calls": 0,
        "footage": "locally generated placeholder clips (no stock API used)",
    }

    for folder in (main.OUT, main.FOOT, main.CACHE):
        folder.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    clips = build_placeholder_footage(main, 8)
    plan = build_plan(main)
    for scene, clip in zip(plan["scenes"], clips):
        scene["media_file"] = str(clip)
    main.prepare_opening(plan)

    tts_backend = "disabled_by_flag"
    voice = None
    if not args.no_tts:
        try:
            print("Requesting free Edge TTS narration...")
            voice = main.build_voiceover(plan["script"])
            tts_backend = "edge-tts"
        except Exception as exc:
            print(f"Edge TTS unavailable ({type(exc).__name__}: {str(exc)[:200]}); using stand-in audio.")
            tts_backend = f"stand-in after {type(exc).__name__}"
            voice = None

    if voice is None:
        from sentence_utils import split_sentences
        voice = synthesized_voiceover(main, split_sentences(plan["script"]))
        # Enforce the same 70-90s production window on the stand-in track.
        total = voice["total_duration"]
        if not 70.0 <= total <= 90.0:
            print(f"Stand-in narration is {total:.2f}s; rescaling to 78s for the check.")
            target = 78.0
            scaled = main.OUT / "voiceover_scaled.wav"
            run([
                main.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                "-i", voice["audio_wav"], "-af", f"atempo={total / target:.8f}",
                "-ar", "48000", "-ac", "2", str(scaled),
            ])
            scaled.replace(voice["audio_wav"])
            factor = target / total
            for record in voice["sentences"]:
                record["start_unscaled"] *= factor
                record["end_unscaled"] *= factor
                record["duration_unscaled"] *= factor
                record["start"] = record["start_unscaled"]
                record["end"] = record["end_unscaled"]
                record["duration"] = record["duration_unscaled"]
            voice["sentence_events"] = [
                {"start": r["start_unscaled"], "end": r["end_unscaled"],
                 "duration": r["duration_unscaled"]}
                for r in voice["sentences"]
            ]
            voice["total_duration"] = main.ffprobe_duration(voice["audio_wav"])
            run([
                main.FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                "-i", voice["audio_wav"], "-c:a", "libmp3lame", "-b:a", "192k",
                voice["audio_mp3"],
            ])

    evidence["tts_backend"] = tts_backend
    evidence["narration_seconds"] = round(voice["total_duration"], 3)

    main.attach_voice_timing_to_scenes(plan, voice)
    subtitles = main.build_subtitles_from_voice(voice)
    evidence["subtitles"] = subtitles["alignment"]

    ensure_fontconfig()
    render_started = time.monotonic()
    main.render(plan, voice["sentence_events"], voice["total_duration"])
    evidence["render_seconds"] = round(time.monotonic() - render_started, 1)

    final = Path("documentary.mp4")
    details = ffprobe_json(final)
    streams = details["streams"]
    video = next(s for s in streams if s["codec_type"] == "video")
    audio = next(s for s in streams if s["codec_type"] == "audio")
    evidence["final_video"] = {
        "path": str(final),
        "size_bytes": final.stat().st_size,
        "duration_seconds": round(float(details["format"]["duration"]), 3),
        "video": {
            "codec": video.get("codec_name"),
            "width": video.get("width"),
            "height": video.get("height"),
            "fps": video.get("r_frame_rate"),
            "duration_seconds": round(float(video.get("duration", 0) or 0), 3),
        },
        "audio": {
            "codec": audio.get("codec_name"),
            "sample_rate": audio.get("sample_rate"),
            "channels": audio.get("channels"),
            "duration_seconds": round(float(audio.get("duration", 0) or 0), 3),
        },
        "expected_duration_seconds": round(voice["total_duration"], 3),
        "duration_delta_seconds": round(
            float(details["format"]["duration"]) - voice["total_duration"], 3
        ),
    }
    evidence["total_seconds"] = round(time.monotonic() - started, 1)
    evidence["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")

    evidence_path = main.OUT / "offline_render_evidence.json"
    evidence_path.write_text(json.dumps(evidence, indent=2), encoding="utf-8")

    if not args.keep_footage:
        shutil.rmtree(main.FOOT, ignore_errors=True)

    print(json.dumps(evidence, indent=2))
    print(f"Evidence written to {evidence_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main_cli())
