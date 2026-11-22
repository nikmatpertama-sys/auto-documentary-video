
import os
import re
import json
import time
import asyncio
import hashlib
import subprocess
import textwrap
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFont
import edge_tts

# ============================================================
# CONFIG
# ============================================================
OUT = Path("output")
FOOT = OUT / "footage"
VIS = OUT / "visuals"
CACHE = Path(".api-cache")

for folder in (OUT, FOOT, VIS, CACHE):
    folder.mkdir(parents=True, exist_ok=True)

GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip()
PIXABAY_KEY = os.getenv("PIXABAY_API_KEY", "").strip()
TOPIC = os.getenv("INPUT_TOPIC", "").strip()
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
VOICE = os.getenv("TTS_VOICE", "en-US-GuyNeural").strip()
BASE_RATE = os.getenv("TTS_RATE", "-4%").strip()
BASE_PITCH = os.getenv("TTS_PITCH", "-3Hz").strip()

W, H, FPS = 1080, 1920, 30

OPENING_LEAD = 0.75
SENTENCE_GAP = 0.30
HOOK_GAP = 0.70
ENDING_HOLD = 1.25

MIN_CLIP_WIDTH = 640
MAX_CLIP_BYTES = 180 * 1024 * 1024

if not GEMINI_KEY:
    raise RuntimeError("Missing GEMINI_API_KEY GitHub Secret.")
if not PIXABAY_KEY:
    raise RuntimeError("Missing PIXABAY_API_KEY GitHub Secret.")
if not TOPIC:
    raise RuntimeError("Enter a topic in the Run workflow form.")


def run(cmd):
    subprocess.run([str(x) for x in cmd], check=True)


def ffprobe_duration(path):
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path)
        ],
        capture_output=True,
        text=True,
        check=True
    )
    return max(0.0, float(result.stdout.strip()))


def split_sentences(text):
    return [
        x.strip()
        for x in re.findall(r"[^.!?]+[.!?]+|[^.!?]+$", text)
        if x.strip()
    ]


def normalize_words(text):
    return re.findall(r"[a-z0-9']+", str(text).lower())


# ============================================================
# AI DOCUMENTARY PLANNER
# ============================================================

def gemini_plan(topic):
    prompt = f'''Create an original, fact-conscious English mini-documentary
for a vertical 9:16 video.

TOPIC: {topic}

Target spoken length: 145-165 words, approximately 60-75 seconds
including deliberate pauses.

Return ONLY valid JSON in this exact structure:

{{
"title":"...",
"hook":"...",
"script":"...",
"scenes":[
{{
"narration":"exact consecutive sentence(s) copied verbatim from script",
"visual_query":"specific searchable real-world footage phrase, 3-6 words",
"visual_queries":["alternate specific shot query","second alternate shot query"],
"visual_type":"footage or diagram",
"diagram_kind":"process, map, comparison, timeline, or data",
"diagram_title":"short title",
"diagram_text":"one concise explanatory line",
"diagram_steps":["short step 1","short step 2","short step 3"],
"source_label":"verified institution name or empty",
"source_url":"official URL or empty"
}}
]
}}

Requirements:

- Exactly 8 scenes, in the same order as the script.
- Scene 1 MUST be footage, not a diagram.
- Its first sentence is a powerful curiosity hook, max 12 words.
- Scene narration must be exact consecutive sentences from script.
- All script sentences must appear in exactly one scene, in order.
- Use 2 or 3 diagram scenes maximum.
- Diagrams must explain a real mechanism, process, comparison,
  timeline, or geographic relationship.
- Other scenes must describe concrete real-world footage.
- Include subject, environment, and/or camera angle in visual queries.
- Tell one coherent story with a reveal and satisfying payoff.
- Avoid list-like narration and generic introductions.
- Vary sentence length and cadence.
- Write for a warm, deep, calm male documentary narrator.
- Use punctuation to indicate natural breaths.
- Never invent statistics, exact quantities, sources, or URLs.
- Leave source fields empty unless genuinely certain.
- End with a memorable final thought, not a call to action.
'''

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{MODEL}:generateContent"
    )

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.55
        }
    }

    last_error = None
    response = None

    for attempt in range(1, 4):
        try:
            response = requests.post(
                url,
                params={"key": GEMINI_KEY},
                json=payload,
                timeout=(20, 180)
            )

            if response.status_code == 429:
                raise RuntimeError(
                    "Gemini API quota/rate limit reached (HTTP 429). "
                    "Wait for quota reset or change API provider/model."
                )

            if response.status_code in (408, 500, 502, 503, 504):
                last_error = (
                    f"HTTP {response.status_code}: "
                    f"{response.text[:500]}"
                )

                if attempt < 3:
                    wait = 10 * attempt
                    print(
                        f"Gemini temporarily unavailable; "
                        f"retrying in {wait}s ({attempt}/3)."
                    )
                    time.sleep(wait)
                    continue

            response.raise_for_status()
            break

        except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError
        ) as exc:
            last_error = str(exc)

            if attempt < 3:
                time.sleep(10 * attempt)
                continue

        except RuntimeError:
            raise

    if response is None or response.status_code >= 400:
        raise RuntimeError(
            f"Gemini planning failed: "
            f"{last_error or response.text[:1000]}"
        )

    data = response.json()

    try:
        raw = data["candidates"][0]["content"]["parts"][0]["text"]
        plan = json.loads(raw)
    except Exception as exc:
        raise RuntimeError(
            f"Gemini returned invalid JSON: {exc}; "
            f"response={str(data)[:1000]}"
        )

    plan["script"] = re.sub(
        r"\s+", " ", str(plan.get("script", ""))
    ).strip()

    plan["scenes"] = plan.get("scenes", [])[:8]

    if len(plan["scenes"]) != 8:
        raise RuntimeError(
            "AI returned an incomplete plan. Expected 8 scenes."
        )

    if len(plan["script"].split()) < 100:
        raise RuntimeError("Documentary script is too short.")

    plan["scenes"][0]["visual_type"] = "footage"

    assign_scene_sentence_ranges(plan)

    Path("plan.json").write_text(
        json.dumps(plan, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

    print(
        f"Script ready: {len(plan['script'].split())} words, "
        f"{len(plan['scenes'])} scenes."
    )

    return plan


def assign_scene_sentence_ranges(plan):
    """Map each scene to exact script sentences."""

    sentences = split_sentences(plan["script"])

    normalized = [
        " ".join(normalize_words(s))
        for s in sentences
    ]

    cursor = 0

    for index, scene in enumerate(plan["scenes"]):
        narration = str(scene.get("narration", "")).strip()
        target = " ".join(normalize_words(narration))

        if not target:
            raise RuntimeError(
                f"Scene {index + 1} has no narration."
            )

        target_words = target.split()
        found = None

        for start in range(cursor, len(sentences)):
            joined = []

            for end in range(
                start,
                min(len(sentences), start + 5)
            ):
                joined.extend(normalized[end].split())

                if joined == target_words:
                    found = (start, end)
                    break

                if len(joined) >= len(target_words):
                    break

            if found:
                break

        if not found:
            raise RuntimeError(
                f"Scene {index + 1} narration is not an exact "
                "consecutive passage from the script. "
                "Regenerate the plan."
            )

        scene["sentence_start"] = found[0]
        scene["sentence_end"] = found[1]

        cursor = found[1] + 1

    if cursor != len(sentences):
        raise RuntimeError(
            "Some script sentences are not assigned to a scene."
        )


# ============================================================
# FOOTAGE SEARCH AND RELEVANCE
# ============================================================

def download_stream(url, dest):
    with requests.get(
        url,
        stream=True,
        timeout=(25, 180),
        headers={
            "User-Agent": "HybridDocumentaryPipeline/2.0"
        }
    ) as response:

        response.raise_for_status()
        total = 0

        with open(dest, "wb") as f:
            for chunk in response.iter_content(1024 * 1024):
                if chunk:
                    total += len(chunk)

                    if total > MAX_CLIP_BYTES:
                        raise RuntimeError(
                            "Clip exceeded 180 MB safety limit."
                        )

                    f.write(chunk)

    if Path(dest).stat().st_size < 50000:
        Path(dest).unlink(missing_ok=True)
        raise RuntimeError(
            "Downloaded clip is unexpectedly small."
        )


def pixabay_search(query):
    query = re.sub(
        r"[^a-zA-Z0-9 ]", " ", query
    ).strip()[:95]

    cache_file = CACHE / (
        hashlib.sha256(
            query.lower().encode()
        ).hexdigest() + ".json"
    )

    if (
        cache_file.exists()
        and time.time() - cache_file.stat().st_mtime < 86400
    ):
        try:
            return json.loads(
                cache_file.read_text(encoding="utf-8")
            ).get("hits", [])
        except Exception:
            pass

    response = requests.get(
        "https://pixabay.com/api/videos/",
        params={
            "key": PIXABAY_KEY,
            "q": query,
            "per_page": 20,
            "safesearch": "true",
            "video_type": "film",
            "order": "popular"
        },
        timeout=35
    )

    if response.status_code == 429:
        raise RuntimeError("Pixabay rate limit reached.")

    response.raise_for_status()

    data = response.json()

    cache_file.write_text(
        json.dumps(data),
        encoding="utf-8"
    )

    return data.get("hits", [])


def relevance_score(hit, query, narration):
    """
    Rank footage using Pixabay metadata.
    This is metadata matching, not AI visual recognition.
    """

    tags = set(normalize_words(hit.get("tags", "")))
    q = set(normalize_words(query))
    n = set(normalize_words(narration))

    stop = {
        "the", "and", "for", "with", "from", "into",
        "that", "this", "over", "under", "through",
        "about", "near", "shot", "video", "aerial",
        "cinematic"
    }

    q -= stop
    n -= stop

    if not tags:
        return 0.0

    q_score = len(q & tags) / max(1, len(q))

    n_score = (
        len(n & tags) /
        max(1, min(len(n), 10))
    )

    return (
        0.72 * q_score +
        0.28 * min(1.0, n_score)
    )


def fetch_footage(plan):
    credits = []
    used = set()

    for i, scene in enumerate(plan["scenes"], 1):

        if (
            scene.get("visual_type") == "diagram"
            and i != 1
        ):
            print(
                f"Scene {i}: explanatory motion graphic selected."
            )
            continue

        queries = (
            [scene.get("visual_query", "")]
            + list(scene.get("visual_queries", [])[:2])
        )

        candidates = {}

        for query in queries:
            if not str(query).strip():
                continue

            try:
                hits = pixabay_search(query)

                for hit in hits:
                    mid = hit.get("id")

                    if not mid or mid in used:
                        continue

                    videos = hit.get("videos", {})

                    rendition = (
                        videos.get("large")
                        or videos.get("medium")
                        or videos.get("small")
                        or {}
                    )

                    url = rendition.get("url")
                    width = int(rendition.get("width") or 0)

                    if not url or width < MIN_CLIP_WIDTH:
                        continue

                    clip_seconds = float(
                        hit.get("duration") or 0
                    )

                    if clip_seconds and clip_seconds < 5:
                        continue

                    score = relevance_score(
                        hit,
                        query,
                        scene.get("narration", "")
                    )

                    old = candidates.get(mid)

                    if old is None or score > old[0]:
                        candidates[mid] = (
                            score,
                            hit,
                            url
                        )

            except Exception as exc:
                print(
                    f"Scene {i}: search issue for "
                    f"'{query}': {exc}"
                )

        ranked = sorted(
            candidates.values(),
            key=lambda x: x[0],
            reverse=True
        )

        chosen = next(
            (x for x in ranked if x[0] >= 0.12),
            None
        )

        if chosen:
            score, hit, url = chosen

            destination = FOOT / f"scene_{i:02d}.mp4"

            try:
                download_stream(url, destination)

                used.add(hit["id"])

                scene["media_file"] = str(destination)

                credits.append(
                    f"Scene {i} | Pixabay | "
                    f"Creator: {hit.get('user', 'Unknown')} | "
                    f"Source: {hit.get('pageURL', '')} | "
                    f"Metadata relevance: {score:.2f} | "
                    "License: Pixabay Content License"
                )

                print(
                    f"Scene {i}: selected footage "
                    f"(metadata match {score:.2f}) — "
                    f"{hit.get('tags', '')[:90]}"
                )

            except Exception as exc:
                print(
                    f"Scene {i}: selected clip could not "
                    f"be downloaded: {exc}"
                )

        else:
            if i == 1:
                raise RuntimeError(
                    "No sufficiently relevant opening footage "
                    "was found. Refusing to publish an unrelated "
                    "or frozen opening. Try another topic wording "
                    "or retry the workflow."
                )

            scene["visual_type"] = "diagram"

            print(
                f"Scene {i}: no sufficiently relevant stock clip; "
                "using animated explainer instead."
            )

    for i, scene in enumerate(plan["scenes"], 1):
        label = str(
            scene.get("source_label") or ""
        ).strip()

        url = str(
            scene.get("source_url") or ""
        ).strip()

        if label and url.startswith("https://"):
            credits.append(
                f"Explainer source for scene {i} | "
                f"{label} | {url}"
            )

    Path("credits.txt").write_text(
        "\n".join(credits)
        if credits
        else "No external footage/source references were verified.\n",
        encoding="utf-8"
    )

    Path("plan.json").write_text(
        json.dumps(plan, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


# ============================================================
# ANIMATED DIAGRAMS
# ============================================================

def get_font(size, bold=False):
    paths = [
        (
            "/usr/share/fonts/truetype/dejavu/"
            "DejaVuSans-Bold.ttf"
            if bold else
            "/usr/share/fonts/truetype/dejavu/"
            "DejaVuSans.ttf"
        ),
        (
            "/usr/share/fonts/truetype/liberation2/"
            "LiberationSans-Bold.ttf"
            if bold else
            "/usr/share/fonts/truetype/liberation2/"
            "LiberationSans-Regular.ttf"
        )
    ]

    for path in paths:
        if Path(path).exists():
            return ImageFont.truetype(path, size)

    return ImageFont.load_default()


def make_diagram(scene, idx):
    """Generate a lightweight animated explainer."""

    width, height, frames = 540, 960, 48

    kind = str(
        scene.get("diagram_kind") or "process"
    ).lower()

    if kind not in {
        "process", "map", "comparison", "timeline", "data"
    }:
        kind = "process"

    title = str(
        scene.get("diagram_title") or "The hidden connection"
    )[:64]

    body = str(
        scene.get("diagram_text") or
        "A process revealed step by step"
    )[:120]

    steps = [
        str(x)[:30]
        for x in (scene.get("diagram_steps") or [])[:4]
    ]

    if len(steps) < 2:
        steps = [
            "The trigger",
            "The process",
            "The result"
        ]

    accent = (73, 204, 218)
    bg = (7, 18, 29)

    directory = VIS / f"diagram_{idx:02d}"
    directory.mkdir(parents=True, exist_ok=True)

    ft = get_font(30, True)
    fb = get_font(19)
    fs = get_font(15, True)

    for frame in range(frames):
        t = frame / (frames - 1)

        im = Image.new("RGB", (width, height), bg)
        d = ImageDraw.Draw(im)

        sweep_x = int((t * 760) - 110)

        d.rectangle(
            (sweep_x, 0, sweep_x + 70, height),
            fill=(9, 25, 37)
        )

        for x in range(-40, width + 40, 50):
            xx = (x + int(frame * 1.2)) % width

            d.line(
                (xx, 0, xx, height),
                fill=(13, 31, 43),
                width=1
            )

        d.text(
            (32, 42),
            f"FIELD EXPLAINER / {idx:02d}",
            font=fs,
            fill=accent
        )

        d.multiline_text(
            (32, 100),
            "\n".join(textwrap.wrap(title, 25)),
            font=ft,
            fill=(245, 248, 250),
            spacing=5
        )

        d.line(
            (32, 205, 508, 205),
            fill=(43, 82, 101),
            width=2
        )

        if kind == "timeline":
            y = 470

            d.line(
                (55, y, 485, y),
                fill=(55, 100, 120),
                width=5
            )

            for j, label in enumerate(steps[:4]):
                x = 60 + j * (
                    420 / max(1, len(steps) - 1)
                )

                active = max(
                    0,
                    min(1, t * len(steps) - j)
                )

                r = 9 + int(8 * active)

                d.ellipse(
                    (x-r, y-r, x+r, y+r),
                    fill=accent if active > 0 else (45, 75, 90)
                )

                d.multiline_text(
                    (x-48, y+42),
                    "\n".join(textwrap.wrap(label, 11)),
                    font=fs,
                    fill=(235, 242, 246),
                    spacing=4,
                    align="center"
                )

        elif kind == "comparison":
            for j, label in enumerate(steps[:2]):
                x = 32 + j * 245

                d.rounded_rectangle(
                    (x, 310, x+220, 650),
                    radius=18,
                    fill=(13, 35, 49),
                    outline=(
                        accent if t > j*.22 else (45, 75, 90)
                    ),
                    width=3
                )

                d.text(
                    (x+18, 340),
                    "A" if j == 0 else "B",
                    font=ft,
                    fill=accent
                )

                d.multiline_text(
                    (x+18, 430),
                    "\n".join(textwrap.wrap(label, 13)),
                    font=fb,
                    fill=(235, 242, 246),
                    spacing=6
                )

                bar = int(
                    160 * max(
                        0,
                        min(1, (t-j*.18)/.65)
                    )
                )

                d.rounded_rectangle(
                    (x+18, 600, x+18+bar, 612),
                    radius=6,
                    fill=accent
                )

        elif kind == "map":
            d.rounded_rectangle(
                (42, 290, 498, 690),
                radius=22,
                fill=(10, 35, 47),
                outline=(38, 89, 105),
                width=2
            )

            points = [
                (90, 620),
                (175, 540),
                (260, 575),
                (345, 420),
                (450, 350)
            ]

            upto = min(
                len(points)-1,
                int(t*(len(points)-1))+1
            )

            for a, b in zip(
                points[:upto+1],
                points[1:upto+2]
            ):
                d.line(
                    (a[0], a[1], b[0], b[1]),
                    fill=accent,
                    width=6
                )

            for j, (x, y) in enumerate(points):
                r = 11 if j <= upto else 7

                d.ellipse(
                    (x-r, y-r, x+r, y+r),
                    fill=accent if j <= upto else (60, 92, 104)
                )

            d.text(
                (62, 720),
                "Illustrative pathway — not to scale",
                font=fs,
                fill=(150, 180, 190)
            )

        elif kind == "data":
            for j, label in enumerate(steps[:4]):
                y = 315 + j * 105

                d.text(
                    (48, y),
                    "\n".join(textwrap.wrap(label, 32)),
                    font=fb,
                    fill=(235, 242, 246)
                )

                bar = int(
                    390 * max(
                        0,
                        min(1, t*1.4-j*.22)
                    )
                )

                d.rounded_rectangle(
                    (48, y+54, 48+max(3, bar), y+67),
                    radius=6,
                    fill=accent
                )

        else:
            n = min(4, len(steps))
            gap = 16
            boxw = (440-gap*(n-1))//n
            y = 380

            for j, label in enumerate(steps[:n]):
                x = 50 + j * (boxw + gap)

                active = max(
                    0,
                    min(1, t*n-j)
                )

                d.rounded_rectangle(
                    (x, y, x+boxw, y+210),
                    radius=14,
                    fill=(13, 37, 51),
                    outline=(
                        accent if active > 0 else (43, 76, 91)
                    ),
                    width=3
                )

                d.text(
                    (x+10, y+18),
                    f"0{j+1}",
                    font=fs,
                    fill=accent
                )

                d.multiline_text(
                    (x+10, y+68),
                    "\n".join(textwrap.wrap(label, 10)),
                    font=fs,
                    fill=(235, 242, 246),
                    spacing=5
                )

                if j < n-1:
                    cx = x + boxw + gap//2
                    cy = y + 105

                    d.polygon(
                        [
                            (cx-6, cy-9),
                            (cx+6, cy),
                            (cx-6, cy+9)
                        ],
                        fill=(
                            accent if t*n > j+1 else (60, 90, 100)
                        )
                    )

        d.multiline_text(
            (38, 790),
            "\n".join(textwrap.wrap(body, 42)),
            font=fb,
            fill=(195, 215, 224),
            spacing=7
        )

        if scene.get("source_label"):
            d.text(
                (38, 900),
                f"SOURCE: {str(scene['source_label'])[:48]}",
                font=fs,
                fill=(125, 165, 178)
            )

        im.save(
            directory / f"frame_{frame:03d}.jpg",
            quality=88,
            optimize=True
        )

    return directory


# ============================================================
# VOICEOVER AND SUBTITLES
# ============================================================

def ass_time(seconds):
    ticks = max(0, int(round(seconds * 100)))

    return (
        f"{ticks // 360000}:"
        f"{(ticks // 6000) % 60:02}:"
        f"{(ticks // 100) % 60:02}."
        f"{ticks % 100:02}"
    )


def build_voice_and_subs(script):
    """Sentence-level TTS with deliberate pauses and word timing."""

    sentences = split_sentences(script)

    if not sentences:
        raise RuntimeError(
            "The script contains no speakable sentences."
        )

    async def generate():
        word_events = []
        sentence_events = []
        wav_parts = []
        cursor = 0.0

        # Visual-only opening before the narrator starts.
        opening_silence = OUT / "opening_silence.wav"

        run([
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", "anullsrc=r=48000:cl=stereo",
            "-t", str(OPENING_LEAD),
            "-c:a", "pcm_s16le",
            str(opening_silence)
        ])

        wav_parts.append(opening_silence)
        cursor += OPENING_LEAD

        for index, sentence in enumerate(sentences):

            rate_values = [
                BASE_RATE, "-5%", "-3%", "-4%"
            ]

            pitch_values = [
                BASE_PITCH, "-2Hz", "-4Hz", "-2Hz"
            ]

            mp3_path = OUT / f"tts_{index:02d}.mp3"
            wav_path = OUT / f"tts_{index:02d}.wav"

            boundaries = []

            communicate = edge_tts.Communicate(
                sentence,
                VOICE,
                rate=rate_values[index % len(rate_values)],
                pitch=pitch_values[index % len(pitch_values)],
                boundary="WordBoundary"
            )

            with open(mp3_path, "wb") as f:
                async for message in communicate.stream():

                    if message["type"] == "audio":
                        f.write(message["data"])

                    elif message["type"] == "WordBoundary":
                        boundaries.append({
                            "text": message["text"],
                            "offset": (
                                message["offset"] / 10_000_000
                            ),
                            "duration": (
                                message["duration"] / 10_000_000
                            )
                        })

            if (
                not mp3_path.exists()
                or mp3_path.stat().st_size < 1000
                or not boundaries
            ):
                raise RuntimeError(
                    f"Edge TTS failed on sentence {index+1}."
                )

            run([
                "ffmpeg", "-y",
                "-i", str(mp3_path),
                "-ar", "48000",
                "-ac", "2",
                "-c:a", "pcm_s16le",
                str(wav_path)
            ])

            duration = ffprobe_duration(wav_path)
            sentence_start = cursor

            for event in boundaries:
                word_events.append({
                    **event,
                    "offset": cursor + event["offset"]
                })

            wav_parts.append(wav_path)
            cursor += duration

            sentence_events.append({
                "start": sentence_start,
                "end": cursor
            })

            gap = (
                HOOK_GAP if index == 0
                else SENTENCE_GAP
            )

            silence = OUT / f"gap_{index:02d}.wav"

            run([
                "ffmpeg", "-y",
                "-f", "lavfi",
                "-i", "anullsrc=r=48000:cl=stereo",
                "-t", str(gap),
                "-c:a", "pcm_s16le",
                str(silence)
            ])

            wav_parts.append(silence)
            cursor += gap

        # Remove the final ordinary sentence gap.
        last_gap = wav_parts.pop()
        last_gap.unlink(missing_ok=True)

        # Replace it with a longer visual ending hold.
        ending = OUT / "ending_hold.wav"

        run([
            "ffmpeg", "-y",
            "-f", "lavfi",
            "-i", "anullsrc=r=48000:cl=stereo",
            "-t", str(ENDING_HOLD),
            "-c:a", "pcm_s16le",
            str(ending)
        ])

        wav_parts.append(ending)

        total_duration = sum(
            ffprobe_duration(p) for p in wav_parts
        )

        concat = OUT / "voice_concat.txt"

        concat.write_text(
            "".join(
                f"file '{p.resolve()}'\n"
                for p in wav_parts
            ),
            encoding="utf-8"
        )

        run([
            "ffmpeg", "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat),
            "-c:a", "pcm_s16le",
            "-ar", "48000",
            "-ac", "2",
            str(OUT / "voiceover.wav")
        ])

        run([
            "ffmpeg", "-y",
            "-i", str(OUT / "voiceover.wav"),
            "-c:a", "libmp3lame",
            "-b:a", "192k",
            str(OUT / "voiceover.mp3")
        ])

        # Word-timed subtitle groups.
        groups = []
        current = []

        for event in word_events:
            current.append(event)

            if (
                len(current) >= 5
                or re.search(r'[.!?]["\']?$', event["text"])
            ):
                groups.append(current)
                current = []

        if current:
            groups.append(current)

        lines = [
            "[Script Info]",
            "Title: Cinematic Documentary Captions",
            "ScriptType: v4.00+",
            "WrapStyle: 2",
            "ScaledBorderAndShadow: yes",
            "PlayResX: 1080",
            "PlayResY: 1920",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
            "Style: Default,DejaVu Sans,49,&H00FFFFFF,&H000000FF,&H00101A25,&H90000000,-1,0,0,0,100,100,0,0,1,4,1,2,75,75,500,1",
            "[Events]",
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"
        ]

        for group in groups:
            start = group[0]["offset"]

            end = max(
                group[-1]["offset"] + group[-1]["duration"],
                start + 0.45
            )

            phrase = " ".join(
                x["text"] for x in group
            )

            phrase = r"\N".join(
                textwrap.wrap(
                    phrase,
                    width=29,
                    break_long_words=False
                )
            )

            phrase = (
                phrase.replace("{", r"\{")
                .replace("}", r"\}")
            )

            lines.append(
                f"Dialogue: 0,{ass_time(start)},"
                f"{ass_time(end)},Default,,0,0,0,,{phrase}"
            )

        (OUT / "captions.ass").write_text(
            "\n".join(lines),
            encoding="utf-8"
        )

        Path("timing.json").write_text(
            json.dumps({
                "sentences": sentence_events,
                "total_duration": total_duration
            }, indent=2),
            encoding="utf-8"
        )

        return sentence_events, total_duration

    return asyncio.run(generate())


# ============================================================
# SCENE TIMING
# ============================================================

def scene_durations(plan, sentence_events, total_duration):
    """
    Each scene owns the pause after its last sentence.
    No more equal-duration scene splitting.
    """

    result = []

    for index, scene in enumerate(plan["scenes"]):
        start_i = scene["sentence_start"]

        start = sentence_events[start_i]["start"]

        if index + 1 < len(plan["scenes"]):
            next_start_i = (
                plan["scenes"][index + 1]["sentence_start"]
            )

            end = sentence_events[next_start_i]["start"]

        else:
            end = total_duration

        if end <= start:
            raise RuntimeError(
                "Invalid scene timing generated from sentence map."
            )

        result.append((start, end, end-start))

    # Show the opening visual during the lead-in silence.
    first_end = result[0][1]

    result[0] = (
        0.0,
        first_end,
        first_end
    )

    return result


# ============================================================
# VIDEO RENDERING
# ============================================================

def render_footage_scene(
    source,
    duration,
    output,
    hook_text=None
):
    source_duration = ffprobe_duration(source)

    loop_args = (
        ["-stream_loop", "-1"]
        if source_duration < duration + 0.1
        else []
    )

    vf = (
        f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,"
        f"crop={W}:{H}:"
        f"x='(iw-ow)/2+(iw-ow)*0.08*sin(t*0.22)':"
        f"y='(ih-oh)/2+(ih-oh)*0.08*cos(t*0.18)':eval=frame,"
        f"fps={FPS},setsar=1,format=yuv420p"
    )

    if hook_text:
        hook_file = OUT / "hook.txt"
        hook_file.write_text(
            hook_text,
            encoding="utf-8"
        )

        vf += (
            ",drawbox=x=0:y=170:w=1080:h=420:"
            "color=black@0.38:t=fill:"
            "enable='between(t,0.6,4.5)'"

            f",drawtext=fontfile=/usr/share/fonts/truetype/dejavu/"
            f"DejaVuSans-Bold.ttf:textfile={hook_file}:"
            "fontcolor=white:fontsize=66:line_spacing=14:"
            "borderw=2:bordercolor=black@0.65:"
            "x=70:y=260:"
            "alpha='if(lt(t,1.0),(t-0.6)/0.4,"
            "if(gt(t,4.1),(4.5-t)/0.4,1))':"
            "enable='between(t,0.6,4.5)'"
        )

    cmd = [
        "ffmpeg", "-y",
        *loop_args,
        "-i", str(source),
        "-t", f"{duration:.3f}",
        "-vf", vf,
        "-an",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-r", str(FPS),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output)
    ]

    run(cmd)


def render_diagram_scene(
    scene,
    idx,
    duration,
    output
):
    frames_dir = make_diagram(scene, idx)

    run([
        "ffmpeg", "-y",
        "-framerate", "24",
        "-i", str(frames_dir / "frame_%03d.jpg"),
        "-t", f"{duration:.3f}",
        "-vf",
        f"scale={W}:{H}:flags=lanczos,"
        f"fps={FPS},format=yuv420p",
        "-an",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        str(output)
    ])


def render(plan, sentence_events, total_duration):
    spans = scene_durations(
        plan,
        sentence_events,
        total_duration
    )

    segment_files = []

    for i, (scene, (_, _, duration)) in enumerate(
        zip(plan["scenes"], spans),
        1
    ):
        duration = max(0.8, duration)

        output = OUT / f"segment_{i:02d}.mp4"

        source = scene.get("media_file")

        if source and Path(source).exists():
            hook = None

            if i == 1:
                hook = re.sub(
                    r"[^A-Za-z0-9 ,.!?\-]",
                    "",
                    str(plan.get("hook") or "")
                )[:90]

            render_footage_scene(
                source,
                duration,
                output,
                hook
            )

        else:
            if i == 1:
                raise RuntimeError(
                    "Opening footage is missing. "
                    "Refusing to render a frozen or unrelated opening."
                )

            render_diagram_scene(
                scene,
                i,
                duration,
                output
            )

        segment_files.append(output)

        print(
            f"Scene {i}/8 rendered: {duration:.2f}s"
        )

    concat = OUT / "video_concat.txt"

    concat.write_text(
        "".join(
            f"file '{p.resolve()}'\n"
            for p in segment_files
        ),
        encoding="utf-8"
    )

    base = OUT / "video_base.mp4"

    run([
        "ffmpeg", "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat),
        "-c", "copy",
        str(base)
    ])

    final = Path("documentary.mp4")

    audio_filter = (
        "[1:a]highpass=f=65,lowpass=f=15000,"
        "acompressor=threshold=-22dB:ratio=2.0:"
        "attack=25:release=250:makeup=1.15,"
        "alimiter=limit=0.88,"
        "loudnorm=I=-16:TP=-1.5:LRA=9,"
        "aresample=48000[a]"
    )

    run([
        "ffmpeg", "-y",
        "-i", str(base),
        "-i", str(OUT / "voiceover.wav"),
        "-filter_complex",
        f"[0:v]subtitles={OUT}/captions.ass[v];{audio_filter}",
        "-map", "[v]",
        "-map", "[a]",
        "-t", f"{total_duration:.3f}",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-r", str(FPS),
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "48000",
        "-ac", "2",
        "-movflags", "+faststart",
        str(final)
    ])

    print(
        f"Final video: {final}; "
        f"duration {ffprobe_duration(final):.2f}s"
    )


# ============================================================
# MAIN
# ============================================================

def main():
    print("1/5 Generating documentary plan...")
    plan = gemini_plan(TOPIC)

    print("2/5 Searching and ranking relevant footage...")
    fetch_footage(plan)

    print("3/5 Generating paced voice-over and subtitles...")
    sentence_events, total_duration = (
        build_voice_and_subs(plan["script"])
    )

    print("4/5 Rendering scenes to actual narration timing...")
    render(
        plan,
        sentence_events,
        total_duration
    )

    print(
        "5/5 Production complete: "
        "documentary.mp4, plan.json, credits.txt, timing.json"
    )


if __name__ == "__main__":
    main()
