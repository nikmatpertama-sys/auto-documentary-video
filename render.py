
import os
import glob
import json
import subprocess

VOICE = "voiceover.mp3"
SUBTITLES = "captions.ass"
OUTPUT = "documentary.mp4"

WIDTH = 1080
HEIGHT = 1920
FPS = 30

clips = sorted(
    glob.glob("footage/*.mp4")
    + glob.glob("footage/*.webm")
    + glob.glob("footage/*.ogv")
    + glob.glob("footage/*.ogg")
)

if not os.path.exists(VOICE):
    raise FileNotFoundError("Voice-over not found!")

if not os.path.exists(SUBTITLES):
    raise FileNotFoundError("ASS subtitle file not found!")

if not clips:
    raise FileNotFoundError("No footage found!")

result = subprocess.run(
    [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json", VOICE
    ],
    capture_output=True,
    text=True,
    check=True
)

duration = float(json.loads(result.stdout)["format"]["duration"])
segment_duration = duration / len(clips)

command = ["ffmpeg", "-y", "-i", VOICE]

for clip in clips:
    command += ["-stream_loop", "-1", "-i", clip]

filters = []

for index in range(len(clips)):
    filters.append(
        f"[{index + 1}:v]"
        f"trim=duration={segment_duration},"
        "setpts=PTS-STARTPTS,"
        f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={WIDTH}:{HEIGHT},"
        f"fps={FPS},setsar=1"
        f"[v{index}]"
    )

concat_inputs = "".join(
    f"[v{index}]" for index in range(len(clips))
)

filters.append(
    f"{concat_inputs}concat=n={len(clips)}:v=1:a=0[base]"
)

filters.append(
    "[base]subtitles=captions.ass[outv]"
)

command += [
    "-filter_complex", ";".join(filters),
    "-map", "[outv]",
    "-map", "0:a:0",
    "-t", str(duration),
    "-c:v", "libx264",
    "-preset", "veryfast",
    "-crf", "23",
    "-pix_fmt", "yuv420p",
    "-c:a", "aac",
    "-b:a", "192k",
    "-movflags", "+faststart",
    OUTPUT
]

subprocess.run(command, check=True)

print("Video successfully rendered with centered subtitles!")
print("Output:", OUTPUT)
