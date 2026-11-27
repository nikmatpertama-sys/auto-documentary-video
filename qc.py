"""MM6 quality-control gate for the rendered vertical documentary.

Every check returns an explicit status so a run can never report success just
because FFmpeg exited 0. Critical failures raise; non-critical findings are
recorded in ``qc_report.json``.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

MIN_DURATION = 70.0
EXPECTED_WIDTH = 1080
EXPECTED_HEIGHT = 1920
MIN_AUDIO_MEAN_DB = -38.0  # catches a silent / near-silent mix
MAX_AUDIO_PEAK_DB = -0.1   # catches clipping


def _ffprobe_json(path, args):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-of", "json", *args, str(path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed for {path}: {result.stderr[-800:]}")
    return json.loads(result.stdout or "{}")


def probe_media(path):
    data = _ffprobe_json(
        path,
        [
            "-show_entries",
            "stream=codec_type,width,height,r_frame_rate,duration:format=duration",
        ],
    )
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fps = 0.0
    raw_rate = str(video.get("r_frame_rate") or "0/1")
    try:
        num, den = raw_rate.split("/", 1)
        fps = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        fps = 0.0
    duration = 0.0
    for candidate in (video.get("duration"), (data.get("format") or {}).get("duration")):
        try:
            duration = float(candidate)
            if duration > 0:
                break
        except (TypeError, ValueError):
            continue
    return {
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "fps": round(fps, 3),
        "duration": round(duration, 3),
        "has_audio": audio is not None,
    }


def probe_loudness(path):
    """Return mean/max volume in dBFS using FFmpeg volumedetect."""
    result = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
         "-vn", "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True,
        text=True,
    )
    text = (result.stderr or "") + (result.stdout or "")
    mean = re.search(r"mean_volume:\s*(-?\d+(?:\.\d+)?) dB", text)
    peak = re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?) dB", text)
    return {
        "mean_db": float(mean.group(1)) if mean else None,
        "peak_db": float(peak.group(1)) if peak else None,
    }


def detect_black_frames(path, min_duration=0.5, pixel_threshold=0.10):
    """Count black stretches; a long black gap means a broken render."""
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostats", "-i", str(path),
            "-vf", f"blackdetect=d={min_duration}:pix_th={pixel_threshold}",
            "-an", "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
    )
    text = (result.stderr or "") + (result.stdout or "")
    windows = re.findall(
        r"black_start:(\d+(?:\.\d+)?) black_end:(\d+(?:\.\d+)?)", text
    )
    return [
        {"start": float(start), "end": float(end), "duration": round(float(end) - float(start), 3)}
        for start, end in windows
    ]


def count_caption_events(captions_path):
    path = Path(captions_path)
    if not path.is_file():
        return 0
    return sum(
        1 for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("Dialogue:")
    )


def run_qc(final_path, plan, voice_duration, captions_path="output/captions.ass"):
    """Evaluate the finished file. Critical problems raise RuntimeError."""
    media = probe_media(final_path)
    loudness = probe_loudness(final_path)
    blacks = detect_black_frames(final_path)
    caption_events = count_caption_events(captions_path)
    script = str((plan or {}).get("script") or "")
    words = len(script.split())
    scenes = len((plan or {}).get("scenes") or [])

    checks = []

    def add(name, ok, detail, critical=True):
        checks.append({
            "check": name,
            "status": "pass" if ok else ("fail" if critical else "warn"),
            "detail": detail,
        })
        return ok

    add(
        "resolution_1080x1920",
        media["width"] == EXPECTED_WIDTH and media["height"] == EXPECTED_HEIGHT,
        f"{media['width']}x{media['height']}",
    )
    add(
        "duration_minimum_70s",
        media["duration"] >= MIN_DURATION - 0.05,
        f"{media['duration']:.2f}s (minimum {MIN_DURATION:.0f}s)",
    )
    add(
        "duration_follows_voice_over",
        abs(media["duration"] - float(voice_duration)) <= 1.0,
        f"video {media['duration']:.2f}s vs VO {float(voice_duration):.2f}s",
    )
    add("has_audio_track", media["has_audio"], "audio stream present" if media["has_audio"] else "no audio stream")
    add(
        "scripts_words_120_200",
        120 <= words <= 200,
        f"{words} words",
    )
    add(
        "captions_present",
        caption_events > 0,
        f"{caption_events} caption events",
    )
    add(
        "no_black_gap_over_0.5s",
        not blacks,
        "none" if not blacks else f"{len(blacks)} black stretch(es)",
    )
    if loudness["mean_db"] is not None:
        add(
            "audio_not_silent",
            loudness["mean_db"] > MIN_AUDIO_MEAN_DB,
            f"mean {loudness['mean_db']} dBFS",
        )
        add(
            "audio_not_clipping",
            loudness["peak_db"] is not None and loudness["peak_db"] <= MAX_AUDIO_PEAK_DB,
            f"peak {loudness['peak_db']} dBFS",
            critical=False,
        )
    add(
        "scene_count_8",
        scenes == 8,
        f"{scenes} scenes",
        critical=False,
    )

    reused = [
        {"scene": index, "reused_from_scene": scene.get("reused_footage_from_scene")}
        for index, scene in enumerate((plan or {}).get("scenes", []), 1)
        if scene.get("reused_footage_from_scene") is not None
    ]
    add(
        "no_reused_footage_between_scenes",
        not reused,
        "none" if not reused else f"reused in {reused}",
        critical=False,
    )

    report = {
        "video": media,
        "voice_duration": round(float(voice_duration), 3),
        "audio": loudness,
        "black_frames": blacks,
        "caption_events": caption_events,
        "script_words": words,
        "scenes": scenes,
        "checks": checks,
        "status": "fail" if any(c["status"] == "fail" for c in checks) else "pass",
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    failures = [c for c in checks if c["status"] == "fail"]
    if failures:
        raise RuntimeError(
            "Final quality control failed: "
            + "; ".join(f"{c['check']} ({c['detail']})" for c in failures)
        )
    return report


def write_qc_report(report, path="qc_report.json"):
    Path(path).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return Path(path)
