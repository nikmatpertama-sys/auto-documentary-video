
import os
import re
import json
import time
import asyncio
import hashlib
import base64
import tempfile
import subprocess
import textwrap
from pathlib import Path

import requests
import edge_tts

# ============================================================
# CONFIG
# ============================================================
OUT = Path("output")
FOOT = OUT / "footage"
CACHE = Path(".api-cache")

for folder in (OUT, FOOT, CACHE):
    folder.mkdir(parents=True, exist_ok=True)

GROQ_KEY = os.getenv("GROQ_API_KEY", "").strip()
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "").strip()
PIXABAY_KEY = os.getenv("PIXABAY_API_KEY", "").strip()
PEXELS_KEY = os.getenv("PEXELS_API_KEY", "").strip()
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

VISION_REQUIRED = os.getenv(
    "FOOTAGE_VISION_REQUIRED", "true"
).strip().lower() not in ("0", "false", "no", "off")

# Do not stop after checking only five candidates.
VISION_CANDIDATE_LIMIT = max(
    1, min(30, int(os.getenv("FOOTAGE_VISION_CANDIDATE_LIMIT", "26")))
)

try:
    VISION_MIN_CONFIDENCE = float(
        os.getenv("FOOTAGE_VISION_MIN_CONFIDENCE", "0.72")
    )
except ValueError:
    VISION_MIN_CONFIDENCE = 0.72

VISION_MIN_CONFIDENCE = max(
    0.0, min(1.0, VISION_MIN_CONFIDENCE)
)

VISION_MODEL = (
    os.getenv("GEMINI_VISION_MODEL", MODEL).strip() or MODEL
)

GROQ_VISION_MODEL = (
    os.getenv(
        "GROQ_VISION_MODEL",
        "qwen/qwen3.8-27b"
    ).strip()
    or "qwen/qwen3.8-27b"
)

OPENROUTER_VISION_MODEL = (
    os.getenv(
        "OPENROUTER_VISION_MODEL",
        "nvidia/nemotron-3.5-super-vl:free"
    ).strip()
    or "nvidia/nemotron-3.5-super-vl:free"
)

OPENROUTER_VISION_FALLBACK_MODEL = (
    os.getenv(
        "OPENROUTER_VISION_FALLBACK_MODEL",
        "inclusionai/ling-3.2-flash:free"
    ).strip()
    or "inclusionai/ling-3.2-flash:free"
)

# Do not silently use the dynamic router for vision.
if OPENROUTER_VISION_MODEL.lower() == "openrouter/free":
    OPENROUTER_VISION_MODEL = "nvidia/nemotron-3.5-super-vl:free"

VISION_DISABLED_PROVIDERS = set()
VISION_PROVIDER_COOLDOWN_UNTIL = {}


class VisionProviderError(RuntimeError):
    def __init__(self, provider, status_code, detail):
        self.provider = provider
        self.status_code = status_code
        super().__init__(
            f"{provider} visual verification HTTP "
            f"{status_code}: {detail}"
        )


class VisionVerificationError(RuntimeError):
    pass


class VisionResponseError(RuntimeError):
    pass


if not (GROQ_KEY or OPENROUTER_KEY or GEMINI_KEY):
    raise RuntimeError(
        "Missing AI provider key. Add GROQ_API_KEY, "
        "OPENROUTER_API_KEY, or GEMINI_API_KEY GitHub Secret."
    )

if not TOPIC:
    raise RuntimeError("Enter a topic in the Run workflow form.")


def run(cmd):
    """Run a command and surface stderr for diagnostics."""
    result = subprocess.run(
        [str(x) for x in cmd],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    if result.returncode != 0:
        details = (
            result.stderr
            or result.stdout
            or "No diagnostic output."
        ).strip()

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
        for x in re.findall(
            r"[^.!?]+[.!?]+|[^.!?]+$",
            text
        )
        if x.strip()
    ]


def normalize_words(text):
    return re.findall(
        r"[a-z0-9']+",
        str(text).lower()
    )


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
    """Extract the first complete JSON object."""
    if isinstance(raw_text, dict):
        return raw_text

    raw = str(raw_text or "").strip()

    raw = re.sub(
        r"^```(?:json)?\s*",
        "",
        raw,
        flags=re.IGNORECASE
    )

    raw = re.sub(r"\s*```$", "", raw)

    start = -1
    depth = 0
    in_string = False
    escaped = False

    for index, char in enumerate(raw):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True

        elif char == "{":
            if depth == 0:
                start = index
            depth += 1

        elif char == "}":
            if depth > 0:
                depth -= 1

                if depth == 0 and start >= 0:
                    return json.loads(
                        raw[start:index + 1]
                    )

    raise ValueError(
        "Provider response did not contain a complete JSON object."
    )


def post_with_transient_retry(url, retries=1, **kwargs):
    """Retry temporary errors, but never retry HTTP 429."""
    last_error = None
    retries = max(0, int(retries))

    for attempt in range(retries + 1):
        try:
            response = requests.post(url, **kwargs)

            if response.status_code == 429:
                return response

            if response.status_code in (408, 500, 502, 503, 504):
                last_error = RuntimeError(
                    f"Temporary HTTP {response.status_code}: "
                    f"{response.text[:500]}"
                )

                if attempt < retries:
                    print(
                        "Temporary AI service issue; retrying once in 5s..."
                    )
                    time.sleep(5)
                    continue

            return response

        except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        ) as exc:
            last_error = exc

            if attempt < retries:
                print(
                    "AI connection issue; retrying once in 5s..."
                )
                time.sleep(5)
                continue

    if last_error:
        raise last_error

    raise RuntimeError(
        "AI request failed without a response."
    )


def call_openai_compatible(provider, api_key, model, prompt):
    if provider == "Groq":
        url = (
            "https://api.groq.com/openai/v1/chat/completions"
        )

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    else:
        url = (
            "https://openrouter.ai/api/v1/chat/completions"
        )

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": os.getenv(
                "OPENROUTER_SITE_URL",
                "https://github.com/"
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
            {
                "role": "user",
                "content": prompt
            },
        ],
        "temperature": 0.35,
    }

    if provider == "Groq":
        payload["max_completion_tokens"] = 6000
    else:
        payload["max_tokens"] = 6000

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
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ],
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
                "Groq",
                GROQ_KEY,
                GROQ_MODEL,
                prompt
            ),
        ))

    if OPENROUTER_KEY:
        providers.append((
            "OpenRouter",
            lambda: call_openai_compatible(
                "OpenRouter",
                OPENROUTER_KEY,
                OPENROUTER_MODEL,
                prompt
            ),
        ))

    if GEMINI_KEY:
        providers.append((
            "Gemini",
            lambda: call_gemini(
                GEMINI_KEY,
                MODEL,
                prompt
            ),
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

            if (
                not isinstance(candidate["scenes"], list)
                or len(candidate["scenes"]) != 8
            ):
                raise RuntimeError(
                    "AI returned an incomplete plan. "
                    "Expected exactly 8 scenes."
                )

            scene_narrations = []

            for scene_index, scene in enumerate(
                candidate["scenes"],
                1
            ):
                if not isinstance(scene, dict):
                    raise RuntimeError(
                        f"Scene {scene_index} is not a JSON object."
                    )

                narration = re.sub(
                    r"\s+",
                    " ",
                    str(scene.get("narration", ""))
                ).strip()

                if not narration:
                    raise RuntimeError(
                        f"Scene {scene_index} has empty narration."
                    )

                scene["narration"] = narration
                scene_narrations.append(narration)

            candidate["script"] = " ".join(
                scene_narrations
            ).strip()

            word_count = len(
                candidate["script"].split()
            )

            if not 100 <= word_count <= 190:
                raise RuntimeError(
                    f"Scene narration totals {word_count} words; "
                    "expected 100-190."
                )

            candidate["scenes"][0]["visual_type"] = "footage"

            assign_scene_sentence_ranges(candidate)

            plan = candidate

            print(
                f"Documentary plan generated successfully by {provider_name}."
            )
            break

        except Exception as exc:
            message = f"{provider_name}: {exc}"
            errors.append(message)

            print(message)

            if index < len(providers):
                print(
                    "Switching automatically to the next AI provider..."
                )

    if plan is None:
        raise RuntimeError(
            "All configured AI providers failed:\n- "
            + "\n- ".join(errors)
        )

    Path("plan.json").write_text(
        json.dumps(
            plan,
            indent=2,
            ensure_ascii=False
        ),
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
        narration = str(
            scene.get("narration", "")
        ).strip()

        target = " ".join(
            normalize_words(narration)
        )

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
                joined.extend(
                    normalized[end].split()
                )

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
    """Download safely; remove partial files on every failure."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.unlink(missing_ok=True)

    try:
        with requests.get(
            url,
            stream=True,
            timeout=(25, 180),
            headers={
                "User-Agent": "AutoDocumentaryVideo/3.0"
            }
        ) as response:

            response.raise_for_status()

            declared = int(
                response.headers.get("Content-Length") or 0
            )

            if declared > MAX_CLIP_BYTES:
                raise RuntimeError(
                    f"Clip is {declared / 1024 / 1024:.1f} MB, "
                    f"above the {MAX_CLIP_BYTES / 1024 / 1024:.0f} MB limit."
                )

            total = 0

            with open(dest, "wb") as f:
                for chunk in response.iter_content(
                    1024 * 1024
                ):
                    if not chunk:
                        continue

                    total += len(chunk)

                    if total > MAX_CLIP_BYTES:
                        raise RuntimeError(
                            f"Clip exceeded "
                            f"{MAX_CLIP_BYTES / 1024 / 1024:.0f} MB safety limit."
                        )

                    f.write(chunk)

        if dest.stat().st_size < 50000:
            raise RuntimeError(
                "Downloaded clip is unexpectedly small."
            )

    except Exception:
        dest.unlink(missing_ok=True)
        raise


def clean_query(query):
    return re.sub(
        r"[^a-zA-Z0-9 ]",
        " ",
        str(query)
    ).strip()[:95]


def pixabay_search(query):
    if not PIXABAY_KEY:
        return []

    query = clean_query(query)

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
            "per_page": 25,
            "safesearch": "true",
            "video_type": "film",
            "order": "popular"
        },
        timeout=35
    )

    if response.status_code == 429:
        raise RuntimeError(
            "Pixabay rate limit reached."
        )

    response.raise_for_status()

    data = response.json()

    cache_file.write_text(
        json.dumps(data),
        encoding="utf-8"
    )

    results = []

    for hit in data.get("hits", []):
        renditions = hit.get("videos", {})

        rendition = (
            renditions.get("large")
            or renditions.get("medium")
            or renditions.get("small")
            or {}
        )

        if not rendition.get("url"):
            continue

        results.append({
            "source": "Pixabay",
            "id": str(hit.get("id")),
            "url": rendition.get("url"),
            "width": int(rendition.get("width") or 0),
            "duration": float(hit.get("duration") or 0),
            "metadata": " ".join([
                str(hit.get("tags", "")),
                str(hit.get("pageURL", ""))
            ]),
            "tags": str(hit.get("tags", "")),
            "creator": hit.get("user", "Unknown"),
            "page_url": hit.get("pageURL", ""),
            "license": "Pixabay Content License",
        })

    return results


def pexels_search(query):
    if not PEXELS_KEY:
        return []

    response = requests.get(
        "https://api.pexels.com/v1/videos/search",
        headers={
            "Authorization": PEXELS_KEY
        },
        params={
            "query": query,
            "per_page": 20,
            "size": "medium"
        },
        timeout=35
    )

    if response.status_code == 429:
        raise RuntimeError(
            "Pexels API rate limit reached."
        )

    response.raise_for_status()

    results = []

    for video in response.json().get("videos", []):
        files = sorted(
            video.get("video_files", []),
            key=lambda x: (
                0 if x.get("quality") == "hd" else 1,
                abs(int(x.get("width") or 0) - 1920)
            )
        )

        files = [
            f for f in files
            if f.get("link")
            and int(f.get("width") or 0) >= MIN_CLIP_WIDTH
        ]

        if not files:
            continue

        file = min(
            files,
            key=lambda x: int(x.get("width") or 99999)
        )

        slug = re.sub(
            r"[-_/]+",
            " ",
            str(video.get("url", ""))
        )

        results.append({
            "source": "Pexels",
            "id": str(video.get("id")),
            "url": file["link"],
            "width": int(file.get("width") or 0),
            "duration": float(video.get("duration") or 0),
            "metadata": slug,
            "tags": slug,
            "creator": (
                video.get("user") or {}
            ).get("name", "Unknown"),
            "page_url": video.get("url", ""),
            "license": "Pexels License",
        })

    return results


def nasa_search(query):
    """NASA public media API; video assets only."""
    response = requests.get(
        "https://images-api.nasa.gov/search",
        params={
            "q": query,
            "media_type": "video",
            "page_size": 8
        },
        timeout=35
    )

    response.raise_for_status()

    results = []

    for item in response.json().get(
        "collection", {}
    ).get("items", [])[:6]:

        data = (item.get("data") or [{}])[0]
        nasa_id = data.get("nasa_id")

        if not nasa_id:
            continue

        metadata = " ".join([
            str(data.get("title", "")),
            str(data.get("description", "")),
            " ".join(data.get("keywords") or [])
        ])

        try:
            manifest = requests.get(
                f"https://images-api.nasa.gov/asset/{nasa_id}",
                timeout=35
            )

            manifest.raise_for_status()

            assets = manifest.json().get(
                "collection", {}
            ).get("items", [])

        except Exception as exc:
            print(
                f"NASA asset lookup skipped ({nasa_id}): {exc}"
            )
            continue

        urls = [
            str(x.get("href", ""))
            for x in assets
            if str(x.get("href", "")).split(
                "?", 1
            )[0].lower().endswith(
                (".mp4", ".mov", ".m4v")
            )
        ]

        if not urls:
            continue

        urls.sort(
            key=lambda u: (
                0 if re.search(
                    r"(medium|small|low)",
                    u,
                    re.I
                ) else 1,
                len(u)
            )
        )

        results.append({
            "source": "NASA",
            "id": str(nasa_id),
            "url": urls[0],
            "width": MIN_CLIP_WIDTH,
            "duration": 0,
            "metadata": metadata,
            "tags": metadata,
            "creator": data.get("center", "NASA"),
            "page_url": item.get(
                "href",
                f"https://images.nasa.gov/details/{nasa_id}"
            ),
            "license": (
                "NASA media usage guidelines; "
                "verify third-party credits where noted"
            ),
        })

    return results


def commons_search(query):
    """Search Commons video files and keep reusable licenses."""
    response = requests.get(
        "https://commons.wikimedia.org/w/api.php",
        params={
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrsearch": f"filetype:video {query}",
            "gsrnamespace": 6,
            "gsrlimit": 10,
            "prop": "imageinfo",
            "iiprop": "url|size|mime|extmetadata"
        },
        headers={
            "User-Agent": "AutoDocumentaryVideo/3.0 (video research)"
        },
        timeout=35
    )

    response.raise_for_status()

    pages = response.json().get(
        "query", {}
    ).get("pages", {})

    results = []

    for page in pages.values():
        info = (page.get("imageinfo") or [{}])[0]
        mime = str(info.get("mime", ""))

        if not mime.startswith("video/"):
            continue

        ext = info.get("extmetadata") or {}

        license_name = re.sub(
            r"<[^>]+>",
            "",
            str(
                (ext.get("LicenseShortName") or {}).get("value", "")
            )
        )

        license_normalized = (
            license_name.lower()
            .replace(" ", "")
            .replace("_", "-")
        )

        restricted = any(
            term in license_normalized
            for term in (
                "noncommercial",
                "non-commercial",
                "cc-by-nc",
                "ccby-nc",
                "noderivatives",
                "no-derivatives",
                "cc-by-nd",
                "ccby-nd",
            )
        )

        reusable = (
            "cc0" in license_normalized
            or "publicdomain" in license_normalized
            or "public-domain" in license_normalized
            or "ccby" in license_normalized
            or "creativecommonsattribution" in license_normalized
        )

        if restricted or not reusable:
            continue

        url = info.get("url", "")

        if not url:
            continue

        artist = re.sub(
            r"<[^>]+>",
            "",
            str(
                (ext.get("Artist") or {}).get("value", "Unknown")
            )
        )

        description = re.sub(
            r"<[^>]+>",
            " ",
            str(
                (ext.get("ImageDescription") or {}).get("value", "")
            )
        )

        metadata = (
            f"{page.get('title', '')} {description}"
        )

        results.append({
            "source": "Wikimedia Commons",
            "id": str(page.get("pageid")),
            "url": url,
            "width": int(info.get("width") or 0),
            "duration": 0,
            "metadata": metadata,
            "tags": metadata,
            "creator": artist,
            "page_url": (
                page.get("canonicalurl")
                or (
                    "https://commons.wikimedia.org/wiki/"
                    + str(page.get("title", "")).replace(" ", "_")
                )
            ),
            "license": license_name or "See file page",
        })

    return results


def search_all_footage_sources(query):
    """Query all configured/open sources independently."""
    cache_sources = (
        ("pixabay" if PIXABAY_KEY else "")
        + ("|pexels" if PEXELS_KEY else "")
        + "|nasa|commons"
    )

    cache_file = CACHE / (
        hashlib.sha256(
            (
                cache_sources + "|" + query.lower()
            ).encode()
        ).hexdigest() + ".sources.json"
    )

    if (
        cache_file.exists()
        and time.time() - cache_file.stat().st_mtime < 21600
    ):
        try:
            return json.loads(
                cache_file.read_text(encoding="utf-8")
            )
        except Exception:
            pass

    searches = [
        ("Pixabay", pixabay_search),
        ("Pexels", pexels_search),
        ("NASA", nasa_search),
        ("Wikimedia Commons", commons_search),
    ]

    combined = []

    for source, search_fn in searches:
        try:
            found = search_fn(query)
            combined.extend(found)

            if found:
                print(
                    f"  {source}: {len(found)} candidate(s)"
                )

        except Exception as exc:
            print(
                f"  {source} search unavailable "
                f"for '{query}': {exc}"
            )

    try:
        cache_file.write_text(
            json.dumps(combined),
            encoding="utf-8"
        )
    except Exception:
        pass

    return combined


STOP_WORDS = {
    "the", "and", "for", "with", "from", "into", "that", "this",
    "over", "of", "under", "through", "about", "near", "video",
    "cinematic", "footage", "close", "up", "wide", "angle",
    "slow", "motion", "beautiful", "nature", "scene", "view",
    "real", "world", "camera", "showing", "moving", "during",
    "while", "where", "what", "when", "which", "their", "there",
    "they", "these", "those", "onto", "its", "are", "was",
    "were", "been", "being", "has", "have", "had", "will",
    "would", "could", "should", "can", "may", "one", "two",
    "first", "second", "then", "than", "also", "more", "most",
    "very", "some", "many", "such", "like", "make", "made",
    "high", "low", "level", "shot", "shots", "clip"
}


CONCEPT_GROUPS = {
    "rainforest": {
        "amazon", "rainforest", "jungle",
        "tropical", "tropics", "equatorial"
    },
    "canopy": {
        "canopy", "treetop", "treetops",
        "tree", "trees", "foliage", "crown",
        "leaf", "leaves"
    },
    "sunrise": {
        "sunrise", "dawn", "daybreak", "morning",
        "sunlight", "sun", "golden"
    },
    "forest": {
        "forest", "woodland", "woods", "timberland", "trees"
    },
    "aerial": {
        "aerial", "drone", "overhead",
        "topdown", "birdseye", "helicopter", "fromabove"
    },
    "river": {
        "river", "rivers", "waterway", "tributary", "stream"
    },
    "waterfall": {
        "waterfall", "falls", "cascade", "cascades"
    },
    "wildlife": {
        "wildlife", "animal", "animals", "fauna"
    },
    "bird": {
        "bird", "birds", "avian", "parrot",
        "parrots", "macaw", "macaws"
    },
    "mountain": {
        "mountain", "mountains", "mountainous",
        "peak", "peaks", "alpine"
    },
    "ocean": {
        "ocean", "sea", "marine", "underwater"
    },
    "desert": {
        "desert", "arid", "dunes", "sand"
    },
}


CONTEXT_CONFLICTS = {
    "snow_winter": {
        "snow", "snowy", "winter", "ice",
        "icy", "frost", "frozen"
    },
    "autumn": {
        "autumn", "fall", "falling", "maple", "orangeleaves"
    },
    "underwater": {
        "underwater", "submarine", "coral",
        "reef", "fish", "fishes"
    },
    "urban": {
        "urban", "city", "cities", "street",
        "building", "buildings", "traffic"
    },
    "desert": {
        "desert", "dunes", "arid", "sandstorm"
    },
}


def query_concepts(text):
    tokens = set(
        normalize_words(text)
    ) - STOP_WORDS

    concepts = {}

    for name, aliases in CONCEPT_GROUPS.items():
        if tokens & aliases:
            concepts[name] = aliases

    covered = (
        set().union(*concepts.values())
        if concepts
        else set()
    )

    for token in tokens - covered:
        if len(token) > 2:
            concepts[f"term:{token}"] = {token}

    return tokens, concepts


def expand_visual_queries(raw_queries):
    """Create targeted search variants while preserving shot type."""
    expanded = []

    replacements = (
        (r"\bAmazon\b", "tropical rainforest"),
        (r"\bcanopy\b", "treetops"),
        (r"\bsunrise\b", "dawn"),
        (r"\baerial view\b", "drone view"),
    )

    for raw in raw_queries:
        base = clean_query(raw)

        if not base:
            continue

        expanded.append(base)

        variant = base

        for pattern, replacement in replacements:
            variant = re.sub(
                pattern,
                replacement,
                variant,
                flags=re.IGNORECASE
            )

        if variant.lower() != base.lower():
            expanded.append(
                clean_query(variant)
            )

        mood_words = {
            "sunrise", "sunset", "dawn", "dusk",
            "morning", "evening", "golden",
            "cinematic", "slow", "motion"
        }

        core_words = [
            token
            for token in normalize_words(base)
            if token not in STOP_WORDS
            and token not in mood_words
        ]

        core = " ".join(core_words).strip()

        if core and core.lower() != base.lower():
            expanded.append(
                clean_query(core)
            )

    joined = " ".join(expanded).lower()

    if (
        "amazon" in joined
        and ("canopy" in joined or "treetop" in joined)
    ):
        expanded.extend([
            "Amazon rainforest aerial canopy",
            "Amazon rainforest drone canopy",
            "Brazil Amazon rainforest aerial treetops",
            "tropical rainforest canopy drone view",
            "Amazon jungle overhead forest canopy",
        ])

    unique = []
    seen = set()

    for item in expanded:
        key = item.lower()

        if item and key not in seen:
            unique.append(item)
            seen.add(key)

    return unique[:12]


def relevance_score(hit, query, narration):
    """Score subject, setting and requested shot type from metadata."""
    metadata = " ".join([
        str(hit.get("metadata", "")),
        str(hit.get("tags", "")),
        str(hit.get("page_url", "")),
    ])

    metadata_tokens = set(
        normalize_words(metadata)
    )

    query_tokens, query_groups = query_concepts(query)
    narration_tokens, narration_groups = query_concepts(narration)

    if not metadata_tokens or not query_tokens:
        return 0.0, 0, max(1, len(query_groups))

    matched_groups = 0

    for aliases in query_groups.values():
        if metadata_tokens & aliases:
            matched_groups += 1

    direct_hits = query_tokens & metadata_tokens

    group_coverage = (
        matched_groups / max(1, len(query_groups))
    )

    direct_coverage = (
        len(direct_hits) / max(1, len(query_tokens))
    )

    narration_matches = sum(
        1 for aliases in narration_groups.values()
        if metadata_tokens & aliases
    )

    narration_coverage = narration_matches / max(
        1,
        min(6, len(narration_groups))
    )

    # Aerial/drone is not interchangeable with a ground-level clip.
    camera_aliases = CONCEPT_GROUPS.get("aerial", set())

    camera_requested = bool(
        query_tokens & camera_aliases
    )

    camera_match = bool(
        metadata_tokens & camera_aliases
    )

    camera_bonus = (
        0.10
        if camera_requested and camera_match
        else 0.0
    )

    camera_penalty = (
        0.04
        if camera_requested and not camera_match
        else 0.0
    )

    context_tokens = query_tokens | narration_tokens

    conflicts = 0

    for conflict_terms in CONTEXT_CONFLICTS.values():
        if (
            metadata_tokens & conflict_terms
            and not context_tokens & conflict_terms
        ):
            conflicts += 1

    score = (
        0.58 * group_coverage
        + 0.18 * direct_coverage
        + 0.18 * narration_coverage
        + camera_bonus
        - camera_penalty
    )

    if conflicts:
        score -= min(
            0.55,
            0.30 * conflicts
        )

    return (
        max(0.0, score),
        matched_groups,
        max(1, len(query_groups))
    )


def required_visual_anchors(scene):
    """Return non-mood subject and camera groups from the primary query."""
    primary = str(
        scene.get("visual_query", "")
    )

    _, groups = query_concepts(primary)

    # Sunrise is mood/timing metadata; aerial is a key shot-type anchor.
    non_subject = {"sunrise"}

    return {
        name: aliases
        for name, aliases in groups.items()
        if name not in non_subject
    }


def candidate_matches_visual_anchors(hit, scene):
    """Require metadata to support the core scene and shot type."""
    anchors = required_visual_anchors(scene)

    if not anchors:
        return True

    metadata = " ".join([
        str(hit.get("metadata", "")),
        str(hit.get("tags", "")),
        str(hit.get("page_url", "")),
    ])

    tokens = set(
        normalize_words(metadata)
    )

    strict_aliases = {
        "rainforest": {
            "amazon", "rainforest", "jungle",
            "tropical", "tropics", "equatorial"
        },
        "canopy": {
            "canopy", "canopies", "treetop",
            "treetops", "foliage", "crown"
        },
        "aerial": {
            "aerial", "drone", "overhead",
            "topdown", "birdseye", "helicopter", "fromabove"
        },
    }

    matched = 0

    for name, aliases in anchors.items():
        if name.startswith("term:"):
            term = name.split(":", 1)[1]
            has_match = term in tokens
        else:
            match_aliases = strict_aliases.get(
                name,
                aliases
            )
            has_match = bool(
                tokens & match_aliases
            )

        if has_match:
            matched += 1

    required = min(2, len(anchors))

    return matched >= required


def tag_similarity(tags_a, tags_b):
    a = set(normalize_words(tags_a))
    b = set(normalize_words(tags_b))

    if not a or not b:
        return 0.0

    return len(a & b) / max(
        1,
        len(a | b)
    )


def extract_vision_frames(video_path, frame_count=3):
    """Extract evenly spaced frames for visual inspection."""
    duration = ffprobe_duration(video_path)

    if duration <= 0:
        raise RuntimeError(
            "Downloaded candidate has no readable video duration."
        )

    fractions = (
        0.20,
        0.50,
        0.80
    )[:max(2, min(3, frame_count))]

    timestamps = []

    for fraction in fractions:
        timestamp = min(
            max(0.1, duration * fraction),
            max(0.1, duration - 0.1)
        )

        if (
            not timestamps
            or abs(timestamp - timestamps[-1]) >= 0.05
        ):
            timestamps.append(timestamp)

    frame_paths = []

    with tempfile.TemporaryDirectory(
        prefix="footage_vision_"
    ) as temp_dir:

        temp_path = Path(temp_dir)

        for index, timestamp in enumerate(timestamps, 1):
            frame_path = temp_path / f"frame_{index:02d}.jpg"

            result = subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-hide_banner",
                    "-loglevel", "error",
                    "-ss", f"{timestamp:.3f}",
                    "-i", str(video_path),
                    "-frames:v", "1",
                    "-vf", "scale=768:-2",
                    "-q:v", "5",
                    str(frame_path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            if (
                result.returncode != 0
                or not frame_path.exists()
            ):
                detail = (
                    result.stderr
                    or "FFmpeg did not create a frame."
                ).strip()

                raise RuntimeError(
                    f"Could not extract visual-check frame: "
                    f"{detail[-700:]}"
                )

            frame_paths.append(
                frame_path.read_bytes()
            )

    if len(frame_paths) < 2:
        raise RuntimeError(
            "Not enough readable frames for visual verification."
        )

    return frame_paths


def _parse_visual_verdict(provider, raw_text):
    """Parse and validate a strict visual-verification response."""
    try:
        verdict = extract_json_object(raw_text)

    except Exception as exc:
        preview = re.sub(
            r"\s+",
            " ",
            str(raw_text or "")
        ).strip()[:350]

        raise VisionResponseError(
            f"{provider} did not return valid JSON: {exc}; "
            f"response preview={preview!r}"
        ) from exc

    if not isinstance(verdict, dict):
        raise VisionResponseError(
            f"{provider} returned JSON, but it was not an object."
        )

    relevant_value = verdict.get("relevant")

    if not isinstance(relevant_value, bool):
        raise VisionResponseError(
            f"{provider} JSON is missing a boolean 'relevant' field."
        )

    confidence_value = verdict.get("confidence")

    if isinstance(confidence_value, bool):
        raise VisionResponseError(
            f"{provider} returned a boolean instead of numeric confidence."
        )

    try:
        confidence = float(confidence_value)

    except (TypeError, ValueError) as exc:
        raise VisionResponseError(
            f"{provider} JSON is missing numeric 'confidence'."
        ) from exc

    if not 0.0 <= confidence <= 1.0:
        raise VisionResponseError(
            f"{provider} confidence must be between 0 and 1."
        )

    reason = re.sub(
        r"\s+",
        " ",
        str(
            verdict.get(
                "reason",
                "No visual evidence supplied."
            )
        )
    ).strip()

    return relevant_value, confidence, reason


def _verify_with_gemini(frames, prompt):
    parts = [{"text": prompt}]

    for frame in frames[:2]:
        parts.append({
            "inline_data": {
                "mime_type": "image/jpeg",
                "data": base64.b64encode(frame).decode("ascii"),
            }
        })

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{VISION_MODEL}:generateContent"
    )

    payload = {
        "contents": [
            {"parts": parts}
        ],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.1,
            "maxOutputTokens": 512,
        },
    }

    response = post_with_transient_retry(
        url,
        retries=0,
        params={"key": GEMINI_KEY},
        json=payload,
        timeout=(15, 55),
    )

    if response.status_code >= 400:
        detail = response.text[:700].replace("\n", " ")

        raise VisionProviderError(
            "Gemini",
            response.status_code,
            detail
        )

    try:
        data = response.json()

        raw_text = "\n".join(
            part.get("text", "")
            for part in data["candidates"][0]["content"]["parts"]
            if part.get("text")
        )

    except Exception as exc:
        raise VisionResponseError(
            f"Gemini visual verification returned an unreadable payload: "
            f"{exc}; body={response.text[:500]}"
        ) from exc

    return _parse_visual_verdict(
        "Gemini",
        raw_text
    )


def _verify_with_openai_compatible_vision(
    provider,
    api_key,
    model,
    frames,
    prompt
):
    if provider == "Groq":
        url = (
            "https://api.groq.com/openai/v1/chat/completions"
        )

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    else:
        url = (
            "https://openrouter.ai/api/v1/chat/completions"
        )

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": os.getenv(
                "OPENROUTER_SITE_URL",
                "https://github.com/"
            ),
            "X-Title": "Auto Documentary Video",
        }

    content = [
        {
            "type": "text",
            "text": prompt
        }
    ]

    for frame in frames:
        encoded = base64.b64encode(
            frame
        ).decode("ascii")

        content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/jpeg;base64,{encoded}"
            },
        })

    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a strict documentary footage reviewer. "
                    "Return exactly one JSON object with relevant, confidence, "
                    "and reason. Do not add markdown or commentary."
                ),
            },
            {
                "role": "user",
                "content": content
            },
        ],
        "temperature": 0.1,
        "response_format": {
            "type": "json_object"
        },
    }

    if provider == "Groq":
        payload["max_completion_tokens"] = 384

    else:
        payload["max_tokens"] = 768
        payload["reasoning"] = {
            "enabled": False
        }

    response = post_with_transient_retry(
        url,
        retries=0,
        headers=headers,
        json=payload,
        timeout=(15, 65),
    )

    if response.status_code >= 400:
        detail = response.text[:700].replace("\n", " ")

        raise VisionProviderError(
            provider,
            response.status_code,
            detail
        )

    try:
        data = response.json()

        message = data["choices"][0]["message"]
        message_content = message.get("content")

        if isinstance(message_content, list):
            text_parts = []

            for item in message_content:
                if isinstance(item, str):
                    text_parts.append(item)

                elif (
                    isinstance(item, dict)
                    and item.get("text")
                ):
                    text_parts.append(
                        str(item["text"])
                    )

            raw_text = "\n".join(text_parts).strip()

        else:
            raw_text = str(
                message_content or ""
            ).strip()

        if not raw_text:
            choice = data.get("choices", [{}])[0]
            usage = data.get("usage", {})

            raise ValueError(
                "Empty response content "
                f"(model={data.get('model', model)!r}, "
                f"finish_reason={choice.get('finish_reason')!r}, "
                f"completion_tokens={usage.get('completion_tokens')!r})."
            )

    except Exception as exc:
        raise VisionResponseError(
            f"{provider} visual verification returned an unreadable response: "
            f"{exc}; body={response.text[:500]}"
        ) from exc

    return _parse_visual_verdict(
        provider,
        raw_text
    )


def _provider_retry_delay(error_text, default_seconds=60):
    """Read retry timing from common rate-limit messages."""
    text = str(error_text or "")

    patterns = (
        r"try again in\s*([0-9]+(?:\.[0-9]+)?)\s*s",
        r"retry[- ]after[\s:=]+([0-9]+(?:\.[0-9]+)?)",
    )

    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            flags=re.IGNORECASE
        )

        if match:
            try:
                return max(
                    5,
                    min(180, float(match.group(1)))
                )
            except ValueError:
                pass

    return float(default_seconds)


def _provider_cooldown_remaining(provider_id):
    return max(
        0.0,
        VISION_PROVIDER_COOLDOWN_UNTIL.get(
            provider_id,
            0.0
        ) - time.monotonic(),
    )


def verify_footage_visually(
    video_path,
    scene,
    query,
    candidate_metadata=""
):
    """Verify the shot visually, using catalog metadata for provenance."""
    frames = extract_vision_frames(
        video_path,
        frame_count=3
    )

    metadata_context = re.sub(
        r"\s+",
        " ",
        str(candidate_metadata or "")
    ).strip()[:1200]

    prompt = f"""You are a strict documentary footage verifier.
Inspect the attached frames sampled from one candidate video clip.

SCENE VISUAL QUERY: {query}
SCENE NARRATION: {scene.get('narration', '')}
SOURCE CATALOG METADATA: {metadata_context or 'Not supplied'}

IMPORTANT DISTINCTION:
- Judge visible subject, composition, environment and camera viewpoint from the images.
- Use explicit source catalog metadata as supporting provenance for geographic identity.
- Do NOT demand that an image visually proves an exact country, river basin, or rainforest name. A genuine Amazon rainforest clip cannot always be distinguished geographically from another tropical rainforest by pixels alone.
- However, metadata alone cannot turn an unrelated image into a match. The visible footage must still show the requested type of scene.

VISUAL MATCH RULES:
- If the query asks for an aerial/drone canopy view, the frames must show a broad overhead view of dense forest treetops. A ground-level path, close-up leaves, isolated branches, hills, or a stylized animation is not a match.
- If source metadata explicitly identifies Amazon rainforest and the frames visibly show dense tropical forest canopy from above, do not reject merely because the exact Amazon location cannot be recognized visually.
- Treat the visual query as the primary shot requirement. The narration provides context, but do not require every fact in the narration to appear in one clip.
- Do not infer actions or objects that are not visible. If the frames contradict the requested shot, reject.

Return ONLY JSON:
{{"relevant": true, "confidence": 0.0, "reason": "brief visible evidence and metadata basis"}}

Set relevant=true only when the visual composition is a genuine match and source metadata supports any specific geographic claim. If uncertain about the actual shot, set false. Confidence must be a number from 0 to 1."""

    providers = []

    if GROQ_KEY:
        # One representative frame reduces Groq TPM consumption.
        groq_frames = [
            frames[len(frames) // 2]
        ]

        providers.append((
            "Groq",
            "Groq",
            lambda: _verify_with_openai_compatible_vision(
                "Groq",
                GROQ_KEY,
                GROQ_VISION_MODEL,
                groq_frames,
                prompt
            ),
        ))

    if OPENROUTER_KEY:
        models = []

        for model_name in (
            OPENROUTER_VISION_MODEL,
            OPENROUTER_VISION_FALLBACK_MODEL,
        ):
            if (
                model_name
                and model_name not in models
            ):
                models.append(model_name)

        openrouter_frames = (
            [frames[0], frames[-1]]
            if len(frames) > 1
            else frames
        )

        for model_name in models:
            route_id = f"OpenRouter::{model_name}"

            providers.append((
                route_id,
                f"OpenRouter ({model_name})",
                lambda model_name=model_name: (
                    _verify_with_openai_compatible_vision(
                        "OpenRouter",
                        OPENROUTER_KEY,
                        model_name,
                        openrouter_frames,
                        prompt
                    )
                ),
            ))

    if GEMINI_KEY:
        gemini_frames = (
            [frames[0], frames[-1]]
            if len(frames) > 1
            else frames
        )

        providers.append((
            "Gemini",
            f"Gemini ({VISION_MODEL})",
            lambda: _verify_with_gemini(
                gemini_frames,
                prompt
            ),
        ))

    errors = []
    attempted = 0

    for provider_id, provider_name, verify in providers:
        if provider_id in VISION_DISABLED_PROVIDERS:
            print(
                f"Visual verifier: skipping {provider_name}; "
                "route disabled after a hard error."
            )
            continue

        cooldown = _provider_cooldown_remaining(
            provider_id
        )

        if cooldown > 0:
            print(
                f"Visual verifier: {provider_name} cooling down for "
                f"{cooldown:.0f}s; trying the next backup."
            )
            continue

        attempted += 1

        print(
            f"Visual verifier: trying {provider_name}..."
        )

        try:
            relevant, confidence, reason = verify()

            accepted = (
                relevant
                and confidence >= VISION_MIN_CONFIDENCE
            )

            print(
                f"Visual verifier: {provider_name} returned "
                f"{'PASS' if accepted else 'REJECT'} "
                f"({confidence:.2f})."
            )

            return (
                accepted,
                confidence,
                reason,
                provider_name
            )

        except VisionProviderError as exc:
            errors.append(str(exc))

            if exc.status_code == 429:
                delay = _provider_retry_delay(
                    str(exc)
                )

                VISION_PROVIDER_COOLDOWN_UNTIL[
                    provider_id
                ] = time.monotonic() + delay

                print(
                    f"Visual verifier {provider_name} rate-limited; "
                    f"cooling this route for {delay:.0f}s and switching backup."
                )

            elif exc.status_code in (
                400, 401, 403, 404, 413, 415, 422
            ):
                VISION_DISABLED_PROVIDERS.add(
                    provider_id
                )

                print(
                    f"Visual verifier {provider_name} has a hard "
                    f"configuration error (HTTP {exc.status_code}); "
                    "disabling this route."
                )

            else:
                VISION_PROVIDER_COOLDOWN_UNTIL[
                    provider_id
                ] = time.monotonic() + 20

                print(
                    f"Visual verifier {provider_name} unavailable: "
                    f"{str(exc)[:450]}"
                )

        except VisionResponseError as exc:
            errors.append(str(exc))

            VISION_DISABLED_PROVIDERS.add(
                provider_id
            )

            print(
                f"Visual verifier {provider_name} returned unusable output: "
                f"{str(exc)[:450]}. Disabling this route."
            )

        except requests.exceptions.Timeout as exc:
            errors.append(
                f"{provider_name}: timeout: {exc}"
            )

            VISION_PROVIDER_COOLDOWN_UNTIL[
                provider_id
            ] = time.monotonic() + 30

            print(
                f"Visual verifier {provider_name} timed out; "
                "switching backup."
            )

        except Exception as exc:
            errors.append(
                f"{provider_name}: {exc}"
            )

            VISION_PROVIDER_COOLDOWN_UNTIL[
                provider_id
            ] = time.monotonic() + 20

            print(
                f"Visual verifier {provider_name} failed: "
                f"{str(exc)[:450]}"
            )

    if not providers:
        detail = (
            "No visual AI credentials are configured."
        )

    elif attempted == 0:
        detail = (
            "All visual AI routes are disabled or cooling down."
        )

    elif not errors:
        detail = (
            "No provider produced a usable visual-verification response."
        )

    else:
        detail = " | ".join(errors)

    raise VisionVerificationError(
        "All visual verification routes failed for this candidate. "
        + detail
    )


def fetch_footage(plan):
    credits = []
    used_ids = set()
    selected_tag_sets = []

    visual_provider_names = []

    if GROQ_KEY:
        visual_provider_names.append(
            f"Groq ({GROQ_VISION_MODEL})"
        )

    if OPENROUTER_KEY:
        visual_provider_names.append(
            f"OpenRouter ({OPENROUTER_VISION_MODEL})"
        )

        if (
            OPENROUTER_VISION_FALLBACK_MODEL
            != OPENROUTER_VISION_MODEL
        ):
            visual_provider_names.append(
                "OpenRouter fallback "
                f"({OPENROUTER_VISION_FALLBACK_MODEL})"
            )

    if GEMINI_KEY:
        visual_provider_names.append(
            f"Gemini ({VISION_MODEL})"
        )

    if VISION_REQUIRED and not visual_provider_names:
        raise RuntimeError(
            "Stage 1 requires visual verification. Add at least one of "
            "GEMINI_API_KEY, GROQ_API_KEY, or OPENROUTER_API_KEY under "
            "GitHub Settings > Secrets and variables > Actions."
        )

    if not visual_provider_names:
        print(
            "WARNING: FOOTAGE_VISION_REQUIRED=false and no visual AI keys "
            "are configured; using metadata-only footage checks."
        )

    else:
        print(
            "Visual verification enabled with automatic failover: "
            + " -> ".join(visual_provider_names)
            + f". Minimum confidence={VISION_MIN_CONFIDENCE:.2f}."
        )

    for i, scene in enumerate(plan["scenes"], 1):
        scene["visual_type"] = "footage"

        raw_queries = [
            scene.get("visual_query", "")
        ]

        raw_queries.extend(
            list(scene.get("visual_queries", [])[:2])
        )

        queries = expand_visual_queries(
            raw_queries
        )

        candidates = {}

        required_anchors = required_visual_anchors(
            scene
        )

        if required_anchors:
            print(
                f"Scene {i}: metadata anchors required: "
                + ", ".join(required_anchors.keys())
            )

        for query in queries:
            for hit in search_all_footage_sources(query):
                identity = (
                    f"{hit.get('source')}:{hit.get('id')}"
                )

                if (
                    not hit.get("url")
                    or identity in used_ids
                ):
                    continue

                if not candidate_matches_visual_anchors(
                    hit,
                    scene
                ):
                    continue

                width = int(
                    hit.get("width") or 0
                )

                duration = float(
                    hit.get("duration") or 0
                )

                if width and width < MIN_CLIP_WIDTH:
                    continue

                if duration and duration < 4:
                    continue

                score, matched, total = relevance_score(
                    hit,
                    query,
                    scene.get("narration", "")
                )

                required = (
                    1 if total <= 1 else 2
                )

                # Anchors are the hard gate; score ranks the remaining pool.
                if (
                    matched < required
                    or score < 0.32
                ):
                    continue

                metadata_tokens = set(
                    normalize_words(
                        str(hit.get("metadata", ""))
                        + " "
                        + str(hit.get("tags", ""))
                    )
                )

                scene_context = set(
                    normalize_words(
                        scene.get("narration", "")
                    )
                )

                if (
                    metadata_tokens & CONTEXT_CONFLICTS["snow_winter"]
                    and not scene_context & CONTEXT_CONFLICTS["snow_winter"]
                ):
                    continue

                if (
                    metadata_tokens & CONTEXT_CONFLICTS["underwater"]
                    and not scene_context & CONTEXT_CONFLICTS["underwater"]
                ):
                    continue

                tags = str(
                    hit.get("tags")
                    or hit.get("metadata")
                    or ""
                )

                similarity = max(
                    [
                        tag_similarity(tags, old)
                        for old in selected_tag_sets
                    ] or [0.0]
                )

                if similarity > 0.78:
                    continue

                previous = candidates.get(identity)

                if (
                    previous is None
                    or score > previous[0]
                ):
                    candidates[identity] = (
                        score,
                        hit,
                        query,
                        matched,
                        total
                    )

        ranked = sorted(
            candidates.values(),
            key=lambda item: item[0],
            reverse=True
        )

        print(
            f"Scene {i}: {len(candidates)} metadata-qualified "
            f"candidate(s) across {len(queries)} search variants; "
            f"checking up to "
            f"{min(len(ranked), VISION_CANDIDATE_LIMIT)} candidates."
        )

        # Log the top candidates so the next run shows why they ranked highly.
        for preview_index, preview_item in enumerate(
            ranked[:5],
            1
        ):
            (
                preview_score,
                preview_hit,
                preview_query,
                preview_matched,
                preview_total
            ) = preview_item

            print(
                f"  Candidate rank {preview_index}: "
                f"{preview_hit.get('source')} "
                f"ID={preview_hit.get('id')} "
                f"score={preview_score:.2f} "
                f"match={preview_matched}/{preview_total} "
                f"metadata={str(preview_hit.get('metadata') or preview_hit.get('tags') or '')[:160]}"
            )

        destination = FOOT / f"scene_{i:02d}.mp4"

        download_errors = []
        selected = None
        checked = 0

        for (
            score,
            hit,
            query,
            matched,
            total
        ) in ranked[:VISION_CANDIDATE_LIMIT]:

            checked += 1

            try:
                download_stream(
                    hit["url"],
                    destination
                )

                if visual_provider_names:
                    candidate_metadata = " | ".join([
                        str(hit.get("metadata", "")),
                        str(hit.get("tags", "")),
                    ])

                    (
                        visually_relevant,
                        confidence,
                        visual_reason,
                        vision_provider
                    ) = verify_footage_visually(
                        destination,
                        scene,
                        query,
                        candidate_metadata
                    )

                    print(
                        f"Scene {i}: visual check {checked}/"
                        f"{min(len(ranked), VISION_CANDIDATE_LIMIT)} "
                        f"via {vision_provider} — "
                        f"{'PASS' if visually_relevant else 'REJECT'} "
                        f"({confidence:.2f}): "
                        f"{visual_reason[:220]}"
                    )

                    if not visually_relevant:
                        destination.unlink(
                            missing_ok=True
                        )
                        continue

                else:
                    confidence = None
                    vision_provider = "Metadata-only"
                    visual_reason = (
                        "Metadata-only fallback explicitly enabled."
                    )

                selected = (
                    score,
                    hit,
                    query,
                    matched,
                    total,
                    confidence,
                    visual_reason,
                    vision_provider,
                )

                break

            except Exception as exc:
                message = str(exc)

                download_errors.append(
                    f"{hit.get('source')} "
                    f"{hit.get('id')}: {message}"
                )

                print(
                    f"Scene {i}: candidate rejected/skipped "
                    f"({hit.get('source')}): {message[:350]}"
                )

                destination.unlink(
                    missing_ok=True
                )

                if isinstance(exc, VisionVerificationError):
                    raise RuntimeError(
                        "Visual verification cannot continue after automatic "
                        "provider failover. " + message
                    ) from exc

        if selected is None:
            if not ranked:
                reason = (
                    "No candidate passed the metadata relevance filter."
                )

            elif checked >= min(
                len(ranked),
                VISION_CANDIDATE_LIMIT
            ):
                reason = (
                    f"None of the {checked} candidates checked from "
                    f"{len(ranked)} metadata-ranked candidates passed "
                    "visual verification or downloaded successfully."
                )

            else:
                reason = (
                    "No candidate could be downloaded and visually verified."
                )

            if download_errors:
                reason += (
                    " Details: "
                    + " | ".join(download_errors[:4])
                )

            raise RuntimeError(
                f"Scene {i} could not obtain verified relevant footage. "
                f"{reason} Queries tried: {queries}. "
                "No unrelated footage or diagram will be substituted."
            )

        (
            score,
            hit,
            query,
            matched,
            total,
            vision_confidence,
            vision_reason,
            vision_provider,
        ) = selected

        identity = (
            f"{hit.get('source')}:{hit.get('id')}"
        )

        used_ids.add(identity)

        selected_tag_sets.append(
            str(
                hit.get("tags")
                or hit.get("metadata")
                or ""
            )
        )

        scene["media_file"] = str(destination)
        scene["visual_type"] = "footage"
        scene["selected_footage_source"] = hit.get("source")
        scene["selected_footage_tags"] = (
            hit.get("tags")
            or hit.get("metadata", "")
        )
        scene["selected_footage_url"] = hit.get(
            "page_url",
            ""
        )

        scene["visual_verification"] = {
            "method": (
                f"{vision_provider} Vision"
                if vision_confidence is not None
                else "metadata-only fallback"
            ),
            "confidence": vision_confidence,
            "reason": vision_reason,
            "query": query,
        }

        vision_credit = (
            f" | Visual verification: "
            f"{vision_provider} {vision_confidence:.2f} — "
            f"{vision_reason}"
            if vision_confidence is not None
            else (
                " | Visual verification: disabled "
                "(metadata-only fallback)"
            )
        )

        credits.append(
            f"Scene {i} | {hit.get('source')} | "
            f"Creator: {hit.get('creator', 'Unknown')} | "
            f"Source: {hit.get('page_url', '')} | "
            f"Metadata relevance: {score:.2f} "
            f"({matched}/{total} query concepts)"
            f"{vision_credit} | "
            f"License: {hit.get('license', 'Check source page')}"
        )

        print(
            f"Scene {i}: accepted {hit.get('source')} "
            f"metadata={score:.2f}, "
            f"visual confidence="
            f"{vision_confidence if vision_confidence is not None else 'not checked'} — "
            f"{str(hit.get('metadata', ''))[:140]}"
        )

    Path("credits.txt").write_text(
        "\n".join(credits) + "\n",
        encoding="utf-8"
    )

    Path("plan.json").write_text(
        json.dumps(
            plan,
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )


# ============================================================
# VOICEOVER AND SUBTITLES
# ============================================================

def ass_time(seconds):
    ticks = max(
        0,
        int(round(seconds * 100))
    )

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
                rate=rate_values[
                    index % len(rate_values)
                ],
                pitch=pitch_values[
                    index % len(pitch_values)
                ],
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
                HOOK_GAP
                if index == 0
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

        last_gap = wav_parts.pop()
        last_gap.unlink(missing_ok=True)

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
            ffprobe_duration(p)
            for p in wav_parts
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

        groups = []
        current = []

        for event in word_events:
            current.append(event)

            if (
                len(current) >= 5
                or re.search(
                    r'[.!?]["\']?$',
                    event["text"]
                )
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

def scene_durations(
    plan,
    sentence_events,
    total_duration
):
    """Each scene owns the pause after its last sentence."""
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

        result.append((
            start,
            end,
            end - start
        ))

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
                "Rendering stopped rather than substituting a diagram "
                "or unrelated visual."
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
