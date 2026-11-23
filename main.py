
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

GROQ_KEY = os.getenv("GROQ_API_KEY", "").strip()
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip()
PIXABAY_KEY = os.getenv("PIXABAY_API_KEY", "").strip()
TOPIC = os.getenv("INPUT_TOPIC", "").strip()

GROQ_MODEL = os.getenv(
    "GROQ_MODEL", "openai/gpt-oss-120b"
).strip()
OPENROUTER_MODEL = os.getenv(
    "OPENROUTER_MODEL", "openrouter/free"
).strip()
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip()
VOICE = os.getenv("TTS_VOICE", "en-US-GuyNeural").strip()
BASE_RATE = os.getenv("TTS_RATE", "-4%").strip()
BASE_PITCH = os.getenv("TTS_PITCH", "-3Hz").strip()

W, H, FPS = 1080, 1920, 30
X264_PRESET = os.getenv("X264_PRESET", "ultrafast").strip() or "ultrafast"

OPENING_LEAD = 0.75
SENTENCE_GAP = 0.30
HOOK_GAP = 0.70
ENDING_HOLD = 1.25

MIN_CLIP_WIDTH = 640
MAX_CLIP_BYTES = 180 * 1024 * 1024

if not (GROQ_KEY or OPENROUTER_KEY or GEMINI_KEY):
    raise RuntimeError(
        "Missing AI provider key. Add GROQ_API_KEY, "
        "OPENROUTER_API_KEY, or GEMINI_API_KEY GitHub Secret."
    )
if not PIXABAY_KEY:
    raise RuntimeError("Missing PIXABAY_API_KEY GitHub Secret.")
if not TOPIC:
    raise RuntimeError("Enter a topic in the Run workflow form.")


def run(cmd):
    """Run a command and surface stderr so failures are diagnosable in Actions."""
    result = subprocess.run(
        [str(x) for x in cmd],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        details = (result.stderr or result.stdout or "No diagnostic output.").strip()
        raise RuntimeError(
            f"Command failed with exit code {result.returncode}: "
            f"{details[-5000:]}"
        )
    if result.stdout:
        print(result.stdout, end="")


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

def documentary_prompt(topic):
    return f"""Create an original, fact-conscious English mini-documentary
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
"narration":"complete spoken narration segment for this scene",
"visual_query":"specific searchable real-world footage phrase, 3-6 words",
"visual_queries":["alternate specific shot query","second alternate shot query"],
"source_label":"verified institution name or empty",
"source_url":"official URL or empty"
}}
]
}}

Requirements:
- Exactly 8 scenes, in the same order as the script.
- Scene 1 MUST be footage, not a diagram.
- Its first sentence is a powerful curiosity hook, max 12 words.
- Divide the complete spoken story across the 8 scene narration fields, in order.
- Do not omit story content between scenes or repeat whole sentences.
- The combined scene narration should form a coherent 145-165 word script.
- ALL 8 scenes MUST use concrete, relevant real-world footage.
- NEVER request diagrams, charts, infographics, title cards, or abstract graphics.
- Each visual query must name the exact subject being narrated, not merely a broad mood or setting.
- Avoid wildlife or objects unrelated to the specific narration.
- Include subject, environment, and/or camera angle in visual queries.
- Tell one coherent story with a reveal and satisfying payoff.
- Avoid list-like narration and generic introductions.
- Vary sentence length and cadence.
- Write for a warm, deep, calm male documentary narrator.
- Use punctuation to indicate natural breaths.
- Never invent statistics, exact quantities, sources, or URLs.
- Leave source fields empty unless genuinely certain.
- End with a memorable final thought, not a call to action.
"""


def extract_json_object(raw_text):
    """Extract a JSON object even if a provider wraps it in markdown."""
    raw = str(raw_text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw)
    first = raw.find("{")
    last = raw.rfind("}")
    if first < 0 or last <= first:
        raise ValueError("Provider response did not contain a JSON object.")
    return json.loads(raw[first:last + 1])


def post_with_transient_retry(url, **kwargs):
    """Retry temporary network/server failures once; never retry HTTP 429."""
    last_error = None
    for attempt in range(2):
        try:
            response = requests.post(url, **kwargs)
            if response.status_code == 429:
                return response
            if response.status_code in (408, 500, 502, 503, 504):
                last_error = RuntimeError(
                    f"Temporary HTTP {response.status_code}: "
                    f"{response.text[:500]}"
                )
                if attempt == 0:
                    print("Temporary AI service issue; retrying once in 5s...")
                    time.sleep(5)
                    continue
            return response
        except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        ) as exc:
            last_error = exc
            if attempt == 0:
                print("AI connection issue; retrying once in 5s...")
                time.sleep(5)
                continue
    if last_error:
        raise last_error
    raise RuntimeError("AI request failed without a response.")


def call_openai_compatible(provider, api_key, model, prompt):
    if provider == "Groq":
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
    else:
        url = "https://openrouter.ai/api/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": os.getenv(
                "OPENROUTER_SITE_URL", "https://github.com/"
            ),
            "X-Title": "Auto Documentary Video",
        }

    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are an expert documentary writer and visual "
                    "producer. Follow the requested schema exactly. "
                    "Return only one valid JSON object, with no markdown."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.35,
        # Avoid forcing JSON mode: some Groq GPT-OSS models reject it with HTTP 400.
    }

    # A single attempt per provider: quota errors should immediately
    # trigger the next provider instead of wasting time retrying.
    response = post_with_transient_retry(
        url,
        headers=headers,
        json=payload,
        timeout=(20, 180),
    )

    if response.status_code >= 400:
        detail = response.text[:700].replace("\n", " ")
        raise RuntimeError(
            f"{provider} HTTP {response.status_code}: {detail}"
        )

    data = response.json()
    try:
        return extract_json_object(
            data["choices"][0]["message"]["content"]
        )
    except Exception as exc:
        raise RuntimeError(
            f"{provider} returned an unreadable response: {exc}; "
            f"body={str(data)[:700]}"
        ) from exc


def call_gemini(api_key, model, prompt):
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.55,
        },
    }
    response = post_with_transient_retry(
        url,
        params={"key": api_key},
        json=payload,
        timeout=(20, 180),
    )
    if response.status_code >= 400:
        detail = response.text[:700].replace("\n", " ")
        raise RuntimeError(
            f"Gemini HTTP {response.status_code}: {detail}"
        )
    data = response.json()
    try:
        return extract_json_object(
            data["candidates"][0]["content"]["parts"][0]["text"]
        )
    except Exception as exc:
        raise RuntimeError(
            f"Gemini returned an unreadable response: {exc}; "
            f"body={str(data)[:700]}"
        ) from exc


def gemini_plan(topic):
    """Generate the plan with automatic provider failover."""
    prompt = documentary_prompt(topic)

    providers = []
    if GROQ_KEY:
        providers.append((
            "Groq",
            lambda: call_openai_compatible(
                "Groq", GROQ_KEY, GROQ_MODEL, prompt
            ),
        ))
    if OPENROUTER_KEY:
        providers.append((
            "OpenRouter",
            lambda: call_openai_compatible(
                "OpenRouter", OPENROUTER_KEY, OPENROUTER_MODEL, prompt
            ),
        ))
    if GEMINI_KEY:
        providers.append((
            "Gemini",
            lambda: call_gemini(GEMINI_KEY, MODEL, prompt),
        ))

    if not providers:
        raise RuntimeError(
            "No AI provider API key found. Add at least one GitHub "
            "Secret: GROQ_API_KEY, OPENROUTER_API_KEY, or GEMINI_API_KEY."
        )

    errors = []
    plan = None

    for index, (provider_name, generate) in enumerate(providers, 1):
        print(
            f"Trying AI provider {index}/{len(providers)}: "
            f"{provider_name}..."
        )
        try:
            candidate = generate()
            candidate["scenes"] = candidate.get("scenes", [])
            if not isinstance(candidate["scenes"], list) or len(candidate["scenes"]) != 8:
                raise RuntimeError(
                    "AI returned an incomplete plan. Expected exactly 8 scenes."
                )

            # Use scene narration as the canonical script. This avoids rejecting
            # a usable plan when a provider paraphrases its separate script field.
            scene_narrations = []
            for scene_index, scene in enumerate(candidate["scenes"], 1):
                if not isinstance(scene, dict):
                    raise RuntimeError(f"Scene {scene_index} is not a JSON object.")
                narration = re.sub(r"\s+", " ", str(scene.get("narration", ""))).strip()
                if not narration:
                    raise RuntimeError(f"Scene {scene_index} has empty narration.")
                scene["narration"] = narration
                scene_narrations.append(narration)

            candidate["script"] = " ".join(scene_narrations).strip()
            word_count = len(candidate["script"].split())
            if not 100 <= word_count <= 190:
                raise RuntimeError(
                    f"Scene narration totals {word_count} words; expected 100-190."
                )

            candidate["scenes"][0]["visual_type"] = "footage"
            assign_scene_sentence_ranges(candidate)
            plan = candidate
            print(f"Documentary plan generated successfully by {provider_name}.")
            break
        except Exception as exc:
            message = f"{provider_name}: {exc}"
            errors.append(message)
            print(f"{message}")
            if index < len(providers):
                print("Switching automatically to the next AI provider...")

    if plan is None:
        raise RuntimeError(
            "All configured AI providers failed:\\n- "
            + "\\n- ".join(errors)
        )

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
    """Conservative tag-based relevance; the specific query is decisive."""
    tags = set(normalize_words(hit.get("tags", "")))
    stop = {
        "the", "and", "for", "with", "from", "into", "that", "this",
        "over", "under", "through", "about", "near", "shot", "video",
        "aerial", "cinematic", "footage", "drone", "close", "up",
        "wide", "angle", "slow", "motion", "beautiful", "nature"
    }
    query_tokens = set(normalize_words(query)) - stop
    narration_tokens = set(normalize_words(narration)) - stop
    if not tags or not query_tokens:
        return 0.0, 0, 0
    query_hits = query_tokens & tags
    narration_hits = narration_tokens & tags
    query_coverage = len(query_hits) / max(1, len(query_tokens))
    narration_coverage = len(narration_hits) / max(1, min(12, len(narration_tokens)))
    score = 0.82 * query_coverage + 0.18 * narration_coverage
    return score, len(query_hits), len(query_tokens)


def tag_similarity(tags_a, tags_b):
    a = set(normalize_words(tags_a))
    b = set(normalize_words(tags_b))
    if not a or not b:
        return 0.0
    return len(a & b) / max(1, len(a | b))


def fetch_footage(plan):
    credits = []
    used_ids = set()
    selected_tag_sets = []

    for i, scene in enumerate(plan["scenes"], 1):
        scene["visual_type"] = "footage"
        raw_queries = [scene.get("visual_query", "")]
        raw_queries.extend(list(scene.get("visual_queries", [])[:3]))
        queries = []
        for q in raw_queries:
            q = re.sub(r"[^a-zA-Z0-9 ]", " ", str(q)).strip()
            if q and q.lower() not in [x.lower() for x in queries]:
                queries.append(q[:95])

        candidates = {}
        for query in queries:
            try:
                hits = pixabay_search(query)
            except Exception as exc:
                print(f"Scene {i}: search issue for '{query}': {exc}")
                continue

            for hit in hits:
                media_id = hit.get("id")
                if not media_id or media_id in used_ids:
                    continue
                videos = hit.get("videos", {})
                rendition = videos.get("large") or videos.get("medium") or videos.get("small") or {}
                url = rendition.get("url")
                width = int(rendition.get("width") or 0)
                duration = float(hit.get("duration") or 0)
                if not url or width < MIN_CLIP_WIDTH or (duration and duration < 5):
                    continue

                score, matched, total = relevance_score(hit, query, scene.get("narration", ""))
                required = 2 if total >= 3 else 1
                if matched < required or score < 0.30:
                    continue

                tags = str(hit.get("tags", ""))
                similarity = max([tag_similarity(tags, old) for old in selected_tag_sets] or [0.0])
                if similarity > 0.68:
                    continue

                old = candidates.get(media_id)
                if old is None or score > old[0]:
                    candidates[media_id] = (score, hit, url, matched, total)

        ranked = sorted(candidates.values(), key=lambda x: x[0], reverse=True)
        if not ranked:
            raise RuntimeError(
                f"Scene {i} has no footage that meets the strict relevance threshold. "
                f"Refusing to insert unrelated footage or a diagram. "
                f"Visual query: {scene.get('visual_query', '')!r}. "
                "Use a more specific visual query or another footage source."
            )

        score, hit, url, matched, total = ranked[0]
        destination = FOOT / f"scene_{i:02d}.mp4"
        try:
            download_stream(url, destination)
        except Exception as exc:
            raise RuntimeError(f"Scene {i}: relevant clip failed to download: {exc}") from exc

        used_ids.add(hit["id"])
        selected_tag_sets.append(str(hit.get("tags", "")))
        scene["media_file"] = str(destination)
        scene["visual_type"] = "footage"
        scene["selected_footage_tags"] = str(hit.get("tags", ""))

        credits.append(
            f"Scene {i} | Pixabay | Creator: {hit.get('user', 'Unknown')} | "
            f"Source: {hit.get('pageURL', '')} | "
            f"Metadata relevance: {score:.2f} ({matched}/{total} query terms) | "
            "License: Pixabay Content License"
        )
        print(
            f"Scene {i}: accepted footage score={score:.2f}, "
            f"query match={matched}/{total} — {hit.get('tags', '')[:120]}"
        )

    Path("credits.txt").write_text(chr(10).join(credits) + chr(10), encoding="utf-8")
    Path("plan.json").write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")


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
        f"y='(ih-oh)/2+(ih-oh)*0.08*cos(t*0.18)',"
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
        "-preset", X264_PRESET,
        "-crf", "20",
        "-r", str(FPS),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output)
    ]

    run(cmd)


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
            raise RuntimeError(
                f"Scene {i} has no approved real-world footage. "
                "Rendering stopped rather than substituting a diagram or unrelated visual."
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
        "-preset", X264_PRESET,
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
